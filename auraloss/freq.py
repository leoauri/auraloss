import torch
import numpy as np
import librosa.filters
from .utils import apply_reduction

from .perceptual import SumAndDifference, FIRFilter

class SpectralConvergenceLoss(torch.nn.Module):
    """Spectral convergence loss module.

    See [Arik et al., 2018](https://arxiv.org/abs/1808.06719). 
    """
    def __init__(self):
        super(SpectralConvergenceLoss, self).__init__()

    def forward(self, x_mag, y_mag):
        return torch.norm(y_mag - x_mag, p="fro") / torch.norm(y_mag, p="fro")


class LogSTFTMagnitudeLoss(torch.nn.Module):
    """Log STFT magnitude loss module.
    
    See [Arik et al., 2018](https://arxiv.org/abs/1808.06719). 
    """
    def __init__(self):
        super(LogSTFTMagnitudeLoss, self).__init__()

    def forward(self, x_mag, y_mag):
        return torch.nn.functional.l1_loss(torch.log(x_mag), torch.log(y_mag))


class STFTLoss(torch.nn.Module):
    """STFT loss module.
    
    See [Yamamoto et al. 2019](https://arxiv.org/abs/1904.04472).
    
    Args:
        fft_size (int, optional): FFT size in samples. Default: 1024
        hop_size (int, optional): Hop size of the FFT in samples. Default: 256
        win_length (int, optional): Length of the FFT analysis window. Default: 1024
        window (string, optional): Window to apply before FFT, options include:
           ['hann_window', 'bartlett_window', 'blackman_window', 'hamming_window', 'kaiser_window'] 
            Default: 'hann_window'
        w_sc (float, optional): Weight of the spectral convergence loss term. Default: 1.0
        w_mag (float, optional): Weight of the log magnitude loss term. Default: 1.0
        w_phs (float, optional): Weight of the spectral phase loss term (Currently not implemented.). Default: 0.0
        sample_rate (int, optional): Sample rate. Required when scale = 'mel'. Default: None
        scale (string, optional): Optional frequency scaling method, options include:
            ['mel', 'chroma'] 
            Default: None
        n_bins (int, optional): Number of scaling frequency bins. Default: None.
        scale_invariance (bool, optional): Perform an optimal scaling of the target. Default: False
        eps (float, optional): Small epsilon value for stablity. Default: 1e-8
        output (str, optional): Format of the loss returned. 
            'loss' : Return only the raw, aggregate loss term.
            'full' : Return the raw loss, plus intermediate loss terms.
            Default: 'loss'
        reduction (string, optional): Specifies the reduction to apply to the output:
            'none': no reduction will be applied,
            'mean': the sum of the output will be divided by the number of elements in the output, 
            'sum': the output will be summed. 
            Default: 'mean'

    Returns:
        loss: 
            Aggreate loss term. Only returned if output='loss'.
        loss, sc_loss, mag_loss, phs_loss: 
            Aggregate and intermediate loss terms. Only returned if output='full'.
    """
    def __init__(self, 
                 fft_size=1024, 
                 hop_size=256, 
                 win_length=1024, 
                 window="hann_window", 
                 w_sc=1.0,
                 w_mag=1.0,
                 w_phs=0.0,
                 w_real=0.0,
                 w_imag=0.0,
                 sample_rate=None,
                 scale=None,
                 n_bins=None,
                 scale_invariance=False,
                 eps=1e-8,
                 output="loss",
                 reduction="mean"):
        super(STFTLoss, self).__init__()
        self.fft_size = fft_size
        self.hop_size = hop_size
        self.win_length = win_length
        self.window = getattr(torch, window)(win_length)
        self.w_sc = w_sc
        self.w_mag = w_mag
        self.w_phs = w_phs
        self.w_real = w_real  
        self.w_imag = w_imag
        self.sample_rate = sample_rate
        self.scale = scale
        self.n_bins = n_bins
        self.scale_invariance = scale_invariance
        self.eps = eps
        self.output = output
        self.reduction = reduction

        self.spectralconv = SpectralConvergenceLoss()
        self.logstft = LogSTFTMagnitudeLoss()

        # setup mel filterbank
        if self.scale == "mel":
            assert(sample_rate != None) # Must set sample rate to use mel scale
            assert(n_bins <= fft_size) # Must be more FFT bins than Mel bins
            fb = librosa.filters.mel(sample_rate, fft_size, n_mels=n_bins)
            self.fb = torch.tensor(fb).unsqueeze(0)
        elif self.scale == "chroma":
            assert(sample_rate != None) # Must set sample rate to use chroma scale
            assert(n_bins <= fft_size) # Must be more FFT bins than chroma bins
            fb = librosa.filters.chroma(sample_rate, fft_size, n_chroma=n_bins)
            self.fb = torch.tensor(fb).unsqueeze(0)

    def stft(self, x):
        """ Perform STFT.
        Args:
            x (Tensor): Input signal tensor (B, T).

        Returns:
            Tensor: x_mag, x_phs
                Magnitude and phase spectra (B, fft_size // 2 + 1, frames).
        """
        x_stft = torch.stft(x, 
                            self.fft_size, 
                            self.hop_size, 
                            self.win_length, 
                            self.window, 
                            return_complex=True)
        x_mag = torch.sqrt(torch.clamp((x_stft.real ** 2) + (x_stft.imag ** 2), min=self.eps))
        #x_phs = torch.angle(x_stft) currently not implemented
        x_real = (x_stft.real).clamp(min=self.eps)
        x_imag = (x_stft.imag).clamp(min=self.eps)
        return x_mag, None, x_real, x_imag

    def forward(self, x, y):
        # compute the magnitude and phase spectra of input and target
        self.window = self.window.to(x.device)
        x_mag, x_phs, x_real, x_imag = self.stft(x.view(-1,x.size(-1)))
        y_mag, y_phs, y_real, y_imag = self.stft(y.view(-1,y.size(-1)))

        # apply relevant transforms
        if self.scale is not None:
            x_mag = torch.matmul(self.fb, x_mag)
            y_mag = torch.matmul(self.fb, y_mag)

        # normalize scales
        if self.scale_invariance:
            alpha = (x_mag * y_mag).sum([-2,-1]) / ((y_mag ** 2).sum([-2,-1]))
            y_mag = y_mag * alpha.unsqueeze(-1)

        # compute loss terms
        sc_loss = self.spectralconv(x_mag, y_mag)
        mag_loss = self.logstft(x_mag, y_mag)
        loss = (self.w_sc * sc_loss) + (self.w_mag * mag_loss)

        if self.w_real > 0:
            real_loss = self.logstft(x_real, y_real)
            loss += self.w_real * real_loss
        if self.w_imag > 0:
            imag_loss = self.logstft(x_imag, y_imag)
            loss += self.w_imag * imag_loss

        loss = apply_reduction(loss, reduction=self.reduction)

        if self.output == "loss":
            return loss
        elif self.output == "full":
            return loss, sc_loss, mag_loss, phs_loss

class MelSTFTLoss(STFTLoss):
    """ Mel-scale STFT loss module. """
    def __init__(self, 
                 sample_rate,
                 fft_size=1024, 
                 hop_size=256, 
                 win_length=1024, 
                 window="hann_window", 
                 w_sc=1.0,
                 w_mag=1.0,
                 w_phs=0.0,
                 n_mels=128):
        super(MelSTFTLoss, self).__init__(fft_size, 
                                          hop_size, 
                                          win_length, 
                                          window,
                                          w_sc,
                                          w_mag, 
                                          w_phs,
                                          0.0,
                                          0.0,
                                          sample_rate,
                                          "mel",
                                          n_mels)

class ChromaSTFTLoss(STFTLoss):
    """ Chroma-scale STFT loss module. """
    def __init__(self, 
                 sample_rate,
                 fft_size=1024, 
                 hop_size=256, 
                 win_length=1024, 
                 window="hann_window", 
                 w_sc=1.0,
                 w_mag=1.0,
                 w_phs=0.0,
                 n_chroma=12):
        super(ChromaSTFTLoss, self).__init__(fft_size, 
                                          hop_size, 
                                          win_length, 
                                          window,
                                          w_sc,
                                          w_mag, 
                                          w_phs,
                                          0.0,
                                          0.0,
                                          sample_rate,
                                          "chroma",
                                          n_chroma)


class MultiResolutionSTFTLoss(torch.nn.Module):
    """ Multi resolution STFT loss module.
    
    See [Yamamoto et al., 2019](https://arxiv.org/abs/1910.11480)

    Args:
        fft_sizes (list): List of FFT sizes.
        hop_sizes (list): List of hop sizes.
        win_lengths (list): List of window lengths.     
        window (string, optional): Window to apply before FFT, options include:
            'hann_window', 'bartlett_window', 'blackman_window', 'hamming_window', 'kaiser_window'] 
            Default: 'hann_window'
        w_sc (float, optional): Weight of the spectral convergence loss term. Default: 1.0
        w_mag (float, optional): Weight of the log magnitude loss term. Default: 1.0
        w_phs (float, optional): Weight of the spectral phase loss term. Default: 0.0
        sample_rate (int, optional): Sample rate. Required when scale = 'mel'. Default: None
        scale (string, optional): Optional frequency scaling method, options include:
            ['mel', 'chroma'] 
            Default: None
        n_bins (int, optional): Number of mel frequency bins. Default: 128.
        scale_invariance (bool, optional): Perform an optimal scaling of the target. Default: False
    """
    def __init__(self,
                 fft_sizes=[1024, 2048, 512],
                 hop_sizes=[120, 240, 50],
                 win_lengths=[600, 1200, 240],
                 window="hann_window",
                 w_sc=1.0,
                 w_mag=1.0,
                 w_phs=0.0,
                 w_real=0.0,
                 w_imag=0.0,
                 sample_rate=None,
                 scale=None,
                 n_bins=None,
                 scale_invariance=False):
        super(MultiResolutionSTFTLoss, self).__init__()
        assert len(fft_sizes) == len(hop_sizes) == len(win_lengths) # must define all
        self.stft_losses = torch.nn.ModuleList()
        for fs, ss, wl in zip(fft_sizes, hop_sizes, win_lengths):
            self.stft_losses += [STFTLoss(fs, 
                                          ss, 
                                          wl,
                                          window,
                                          w_sc,
                                          w_mag,
                                          w_phs,
                                          w_real,
                                          w_imag,
                                          sample_rate,
                                          scale,
                                          n_bins,
                                          scale_invariance)]

    def forward(self, x, y):
        mrstft_loss = 0.0
        for f in self.stft_losses:
            mrstft_loss += f(x, y)
        mrstft_loss /= len(self.stft_losses)
        return mrstft_loss


class RandomResolutionSTFTLoss(torch.nn.Module):
    """Random resolution STFT loss module.

    See [Steinmetz & Reiss, 2020](https://www.christiansteinmetz.com/s/DMRN15__auraloss__Audio_focused_loss_functions_in_PyTorch.pdf)

    Args:
        resolutions (int): Total number of STFT resolutions.
        min_fft_size (int): Smallest FFT size.
        max_fft_size (int): Largest FFT size.
        min_hop_size (int): Smallest hop size as porportion of window size.
        min_hop_size (int): Largest hop size as porportion of window size.
        window (str): Window function type.
        randomize_rate (int): Number of forwards before STFTs are randomized. 
    """
    def __init__(self,
                 resolutions=3,
                 min_fft_size=16,
                 max_fft_size=32768,
                 min_hop_size=0.1,
                 max_hop_size=1.0,
                 windows=["hann_window", "bartlett_window", "blackman_window", "hamming_window", "kaiser_window"],
                 w_sc=1.0,
                 w_mag=1.0,
                 w_phs=0.0,
                 w_real=0.0,
                 w_imag=0.0,
                 sample_rate=None,
                 scale=None,
                 n_mels=None,
                 randomize_rate=1):
        super(RandomResolutionSTFTLoss, self).__init__()
        self.resolutions = resolutions
        self.min_fft_size = min_fft_size
        self.max_fft_size = max_fft_size
        self.min_hop_size = min_hop_size
        self.max_hop_size = max_hop_size
        self.windows = windows
        self.randomize_rate = randomize_rate
        self.w_sc = w_sc
        self.w_mag = w_mag
        self.w_phs = w_phs
        self.w_real = w_real
        self.w_imag = w_imag
        self.sample_rate = sample_rate
        self.scale = scale
        self.n_mels = n_mels

        self.nforwards = 0
        self.randomize_losses() # init the losses 

    def randomize_losses(self):
        # clear the existing STFT losses
        self.stft_losses = torch.nn.ModuleList()
        for n in range(self.resolutions):
            frame_size = 2 ** np.random.randint(np.log2(self.min_fft_size), np.log2(self.max_fft_size))
            hop_size = int(frame_size * (self.min_hop_size + (np.random.rand() * (self.max_hop_size-self.min_hop_size))))
            window_length = int(frame_size * np.random.choice([1.0, 0.5, 0.25]))
            window = np.random.choice(self.windows)
            self.stft_losses += [STFTLoss(frame_size, 
                                          hop_size, 
                                          window_length, 
                                          window,
                                          self.w_sc,
                                          self.w_mag,
                                          self.w_phs,
                                          self.w_real,
                                          self.w_imag,
                                          self.sample_rate,
                                          self.scale,
                                          self.n_mels)]

    def forward(self, input, target):
        if input.size(-1) <= self.max_fft_size:
            raise ValueError(f"Input length ({input.size(-1)}) must be larger than largest FFT size ({self.max_fft_size}).") 
        elif target.size(-1) <= self.max_fft_size:
            raise ValueError(f"Target length ({target.size(-1)}) must be larger than largest FFT size ({self.max_fft_size}).") 

        if self.nforwards % self.randomize_rate == 0:
            self.randomize_losses()

        loss = 0.0
        for f in self.stft_losses:
            loss += f(input, target)
        loss /= len(self.stft_losses)

        self.nforwards += 1

        return loss


class SumAndDifferenceSTFTLoss(torch.nn.Module):
    """ Sum and difference sttereo STFT loss module.
    
    See [Steinmetz et al., 2020](https://arxiv.org/abs/2010.10291)

    Args:
        fft_sizes (list, optional): List of FFT sizes.
        hop_sizes (list, optional): List of hop sizes.
        win_lengths (list, optional): List of window lengths.
        window (str, optional): Window function type.
        w_sum (float, optional): Weight of the sum loss component. Default: 1.0
        w_diff (float, optional): Weight of the difference loss component. Default: 1.0
        output (str, optional): Format of the loss returned. 
            'loss' : Return only the raw, aggregate loss term.
            'full' : Return the raw loss, plus intermediate loss terms.
            Default: 'loss'
    
    Returns:
        loss: 
            Aggreate loss term. Only returned if output='loss'.
        loss, sum_loss, diff_loss: 
            Aggregate and intermediate loss terms. Only returned if output='full'.
    """
    def __init__(self,
                 fft_sizes=[1024, 2048, 512],
                 hop_sizes=[120, 240, 50],
                 win_lengths=[600, 1200, 240],
                 window="hann_window",
                 w_sum=1.0,
                 w_diff=1.0, 
                 output="loss"):
        super(SumAndDifferenceSTFTLoss, self).__init__()
        self.sd = SumAndDifference() 
        self.w_sum = 1.0
        self.w_diff = 1.0
        self.output = output
        self.mrstft = MultiResolutionSTFTLoss(fft_sizes, 
                                              hop_sizes, 
                                              win_lengths, 
                                              window)

    def forward(self, input, target):
        input_sum, input_diff = self.sd(input)
        target_sum, target_diff = self.sd(target)

        sum_loss = self.mrstft(input_sum, target_sum)
        diff_loss = self.mrstft(input_diff, target_diff)
        loss = ((self.w_sum * sum_loss) + (self.w_diff * diff_loss))/2

        if self.output == "loss":
            return loss
        elif self.output == "full":
            return loss, sum_loss, diff_loss
