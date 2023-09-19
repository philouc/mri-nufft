"""Stacked Operator for NUFFT."""

import numpy as np
import scipy as sp

from .interfaces.base import FourierOperatorBase, proper_trajectory
from .interfaces import get_operator


class MRIStackedNUFFT(FourierOperatorBase):
    """Stacked NUFFT Operator for MRI.

    The dimension of stacking is always the last one.

    Parameters
    ----------
    samples : array-like
        Sample locations in a 2D kspace
    shape: tuple
        Shape of the image.
    z_index: array-like
        Cartesian z index of masked plan.
    backend: str
        Backend to use.
    smaps: array-like
        Sensitivity maps.
    n_coils: int
        Number of coils.
    n_batchs: int
        Number of batchs.
    **kwargs: dict
        Additional arguments to pass to the backend.
    """

    # Developer Notes:
    # Internally the stacked NUFFT operator (self) uses a backend MRI aware NUFFT
    # operator(op), configured as such:
    # - op.smaps=None
    # - op.n_coils = len(self.z_index) ; op.n_batchs = self.n_coils * self.n_batchs.
    # The kspace is organized as a 2D array of shape
    # (self.n_batchs, self.n_coils, self.n_samples) Note that the stack dimension is
    # fused with the samples
    #

    def __init__(
        self, samples, shape, z_index, backend, smaps, n_coils=1, n_batchs=1, **kwargs
    ):
        self.samples = samples.reshape(-1, samples.shape[-1])
        self.shape = shape
        if z_index is None:
            z_index = np.ones(shape[-1], dtype=bool)
        try:
            self.z_index = np.arange(shape[-1])[z_index]
        except IndexError as e:
            raise ValueError(
                "z-index should be a boolean array of length shape[-1], "
                "or an array of integer."
            ) from e

        self.n_coils = n_coils
        self.n_batchs = n_batchs

        self.smaps = smaps
        self.operator = get_operator(backend)(
            samples,
            shape[:-1],
            n_coils=self.n_coils,
            smaps=None,
            **kwargs,
        )

    @property
    def dtype(self):
        """Return dtype."""
        return self.operator.dtype

    @property
    def n_samples(self):
        """Return number of samples."""
        return len(self.samples) * len(self.z_index)

    @staticmethod
    def _fftz(data):
        """Apply FFT on z-axis."""
        # sqrt(2) required for normalization
        return sp.fft.fftshift(
            sp.fft.fft(sp.fft.ifftshift(data, axes=-1), axis=-1, norm="ortho"), axes=-1
        ) / np.sqrt(2)

    @staticmethod
    def _ifftz(data):
        """Apply IFFT on z-axis."""
        # sqrt(2) required for normalization
        return sp.fft.fftshift(
            sp.fft.ifft(sp.fft.ifftshift(data, axes=-1), axis=-1, norm="ortho"), axes=-1
        ) / np.sqrt(2)

    def op(self, data, ksp=None):
        """Forward operator."""
        if self.uses_sense:
            return self._op_sense(data, ksp)
        else:
            return self._op_calibless(data, ksp)

    def _op_sense(self, data, ksp=None):
        """Apply SENSE operator."""
        ksp = ksp or np.zeros(
            (self.n_batchs, self.n_coils, len(self.samples), len(self.z_index)),
            dtype=self.cpx_dtype,
        )
        data_ = data.reshape(self.n_batchs, *self.shape)
        # TODO Add  batch support
        for b in range(self.n_batchs):
            data_c = data_[b] * self.smaps
            ksp_z = self._fftz(data_c)
            ksp_z = ksp_z.reshape(self.n_coils, *self.shape)
            for i, zidx in enumerate(self.z_index):
                self.operator.op(ksp_z[..., zidx], ksp[b, ..., i])
        ksp = ksp.reshape(self.n_batchs, self.n_coils, self.n_samples)
        return ksp

    def _op_calibless(self, data, ksp=None):
        if ksp is None:
            ksp = np.empty(
                (self.n_batchs, self.n_coils, len(self.samples), len(self.z_index)),
                dtype=self.cpx_dtype,
            )
        data_ = data.reshape((self.n_batchs, self.n_coils, *self.shape))
        ksp_z = self._fftz(data_)
        ksp_z = ksp_z.reshape((self.n_batchs, self.n_coils, *self.shape))
        for b in range(self.n_batchs):
            for i, zidx in enumerate(self.z_index):
                self.operator.op(ksp_z[b, ..., zidx], ksp[b, ..., i])
        ksp = ksp.reshape(self.n_batchs, self.n_coils, self.n_samples)
        return ksp

    def adj_op(self, coeffs, img=None):
        """Adjoint operator."""
        coeffs_ = np.reshape(
            coeffs, (self.n_batchs, self.n_coils, len(self.samples), len(self.z_index))
        )
        if self.uses_sense:
            return self._adj_op_sense(coeffs_, img)
        else:
            return self._adj_op_calibless(coeffs_, img)

    def _adj_op_sense(self, coeffs, img):
        imgz = np.zeros(
            (self.n_batchs, self.n_coils, *self.shape), dtype=self.cpx_dtype
        )
        for b in range(self.n_batchs):
            for i, zidx in enumerate(self.z_index):
                self.operator.adj_op(coeffs[b, ..., i], imgz[b, ..., zidx])
        imgz = imgz.reshape(self.n_batchs, self.n_coils, *self.shape)
        imgc = self._ifftz(imgz)
        img = img or np.empty((self.n_batchs, *self.shape), dtype=self.cpx_dtype)
        for b in range(self.n_batchs):
            img[b] = np.sum(imgc[b] * self.smaps.conj(), axis=0)
        return img

    def _adj_op_calibless(self, coeffs, img):
        imgz = np.zeros(
            (self.n_batchs, self.n_coils, *self.shape), dtype=self.cpx_dtype
        )
        for b in range(self.n_batchs):
            for i, zidx in enumerate(self.z_index):
                self.operator.adj_op(coeffs[b, ..., i], imgz[b, ..., zidx])
        imgz = np.reshape(imgz, (self.n_batchs, self.n_coils, *self.shape))
        img = self._ifftz(imgz)
        return img


def traj3d2stacked(samples, dim_z, n_samples=0):
    """Convert a 3D trajectory into a trajectory and the z-stack index.

    Parameters
    ----------
    samples: array-like
        3D trajectory
    dim_z: int
        Size of the z dimension
    n_samples: int, default=0
        Number of samples per shot. If 0, the shot length is determined by counting the
        unique z values.

    Returns
    -------
    tuple
        2D trajectory, z_index

    """
    samples = np.asarray(samples).reshape(-1, 3)
    z_kspace = np.unique(samples[:, 2])

    if n_samples == 0:
        n_samples = np.prod(samples.shape[:-1]) // len(z_kspace)

    traj2D = samples[:n_samples]

    z_kspace = proper_trajectory(z_kspace, "unit").flatten()
    z_index = z_kspace * dim_z + dim_z // 2

    return traj2D, z_index


def stacked2traj3d(samples2d, z_indexes, dim_z):
    """Convert a 2D trajectory and list of z_index into a 3D trajectory.

    Note that the trajectory is flatten in the process.

    Parameters
    ----------
    samples2d: array-like
        2D trajectory
    z_indexes: array-like
        List of z_index
    dim_z: int
        Size of the z dimension

    Returns
    -------
    samples3d: array-like
        3D trajectory
    """
    z_kspace = (z_indexes - dim_z // 2) / dim_z
    # create the equivalent 3d trajectory
    kspace_locs_proper = proper_trajectory(samples2d, normalize="unit")
    nsamples = len(kspace_locs_proper)
    nz = len(z_kspace)
    kspace_locs3d = np.zeros((nsamples * nz, 3))
    # TODO use numpy api for this ?
    for i in range(nsamples):
        kspace_locs3d[i * nz : (i + 1) * nz, :2] = kspace_locs_proper[i]
        kspace_locs3d[i * nz : (i + 1) * nz, 2] = z_kspace

    return kspace_locs3d
