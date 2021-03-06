# coding: utf-8
from __future__ import division

import healpy as hp
import numexpr as ne
import numpy as np
from pyoperators import (
    Cartesian2SphericalOperator, DenseBlockDiagonalOperator, DiagonalOperator,
    IdentityOperator, HomothetyOperator, ReshapeOperator, Rotation2dOperator,
    Rotation3dOperator, Spherical2CartesianOperator)
from pyoperators.utils import (
    operation_assignment, pool_threading, product, split)
from pyoperators.utils.ufuncs import abs2
from pysimulators import (
    BeamGaussian, ConvolutionTruncatedExponentialOperator, Instrument, Layout,
    ProjectionOperator)
from pysimulators.geometry import surface_simple_polygon
from pysimulators.interfaces.healpy import (
    Cartesian2HealpixOperator, HealpixConvolutionGaussianOperator)
from pysimulators.sparse import (
    FSRMatrix, FSRRotation2dMatrix, FSRRotation3dMatrix)
from scipy.constants import c, h, k
from . import _flib as flib
from .calibration import QubicCalibration
from .utils import _compress_mask
from .ripples import ConvolutionRippledGaussianOperator, BeamGaussianRippled

__all__ = ['QubicInstrument',
           'QubicMultibandInstrument']


class Filter(object):
    def __init__(self, nu, relative_bandwidth):
        self.nu = float(nu)
        self.relative_bandwidth = float(relative_bandwidth)
        self.bandwidth = self.nu * self.relative_bandwidth


class Optics(object):
    pass


class SyntheticBeam(object):
    pass


class QubicInstrument(Instrument):
    """
    The QubicInstrument class. It represents the instrument setup.

    """
    def __init__(self, d):
        """
        Parameters
        ----------
        calibration : QubicCalibration
            The calibration tree.
        detector_fknee : array-like, optional
            The detector 1/f knee frequency in Hertz.
        detector_fslope : array-like, optional
            The detector 1/f slope index.
        detector_ncorr : int, optional
            The detector 1/f correlation length.
        detector_ngrids : int, optional
            Number of detector grids.
        detector_nep : array-like, optional
            The detector NEP [W/sqrt(Hz)].
        detector_tau : array-like, optional
            The detector time constants in seconds.
        filter_nu : float, optional
            The filter central wavelength, in Hz.
        filter_relative_bandwidth : float, optional
            The filter relative bandwidth Δν/ν.
        polarizer : boolean, optional
            If true, the polarizer grid is present in the optics setup.
        primary_beam : function f(theta [rad], phi [rad]), optional
            The primary beam transmission function.
        secondary_beam : function f(theta [rad], phi [rad]), optional
            The secondary beam transmission function.
        synthbeam_dtype : dtype, optional
            The data type for the synthetic beams (default: float32).
            It is the dtype used to store the values of the pointing matrix.
        synthbeam_kmax : integer, optional
            The diffraction order above which the peaks are ignored.
            For instance, a value of kmax=2 will model the synthetic beam by
            (2 * kmax + 1)**2 = 25 peaks and a value of kmax=0 will only sample
            the central peak.
        synthbeam_fraction: float, optional
            The fraction of significant peaks retained for the computation
            of the synthetic beam.

        """
        if d['nf_sub'] is None and d['MultiBand']==True:
            raise ValueError, "Error: you want Multiband instrument but you have not specified the number of subband"
        
        filter_nu=d['filter_nu']
        filter_relative_bandwidth=d['filter_relative_bandwidth']
        
        detector_fknee=d['detector_fknee']
        detector_fslope=d['detector_fslope']
        detector_ncorr=d['detector_ncorr']
        detector_nep=d['detector_nep']
        detector_ngrids=d['detector_ngrids']
        detector_tau=d['detector_tau']
        
        polarizer=d['polarizer']
        synthbeam_dtype=np.float32
        synthbeam_fraction=d['synthbeam_fraction']
        synthbeam_kmax=d['synthbeam_kmax']
        synthbeam_peak150_fwhm=np.radians(d['synthbeam_peak150_fwhm'])
        ripples=d['ripples']
        nripples=d['nripples']
        primary_beam=None
        secondary_beam=None
        
        calibration = QubicCalibration(d)
        
        
        self.calibration = calibration
        layout = self._get_detector_layout(detector_ngrids, detector_nep, detector_fknee, detector_fslope,detector_ncorr, detector_tau)
        Instrument.__init__(self, layout)
        self.ripples = ripples
        self.nripples = nripples
        self._init_beams(primary_beam, secondary_beam)
        self._init_filter(filter_nu, filter_relative_bandwidth)
        self._init_horns()
        self._init_optics(polarizer)
        self._init_synthbeam(synthbeam_dtype, synthbeam_peak150_fwhm)
        self.synthbeam.fraction = synthbeam_fraction
        self.synthbeam.kmax = synthbeam_kmax


    def _get_detector_layout(self, ngrids, nep, fknee, fslope, ncorr, tau):
        shape, vertex, removed, index, quadrant, efficiency = \
            self.calibration.get('detarray')
        if ngrids == 2:
            shape = (2,) + shape
            vertex = np.array([vertex, vertex])
            removed = np.array([removed, removed])
            index = np.array([index, index + np.max(index) + 1], index.dtype)
            quadrant = np.array([quadrant, quadrant + 4], quadrant.dtype)
            efficiency = np.array([efficiency, efficiency])
        focal_length = self.calibration.get('optics')['focal length']
        vertex = np.concatenate([vertex, np.full_like(vertex[..., :1], -focal_length)], -1)

        def theta(self):
            return np.arctan2(
                np.sqrt(np.sum(self.center[..., :2]**2, axis=-1)),
                self.center[..., 2])

        def phi(self):
            return np.arctan2(self.center[..., 1], self.center[..., 0])

        layout = Layout(
            shape, vertex=vertex, selection=~removed, ordering=index,
            quadrant=quadrant, nep=nep, fknee=fknee, fslope=fslope,
            tau=tau, theta=theta, phi=phi, efficiency=efficiency)

        # assume all detectors have the same area
        layout.area = surface_simple_polygon(layout.vertex[0, :, :2])
        layout.ncorr = ncorr
        layout.ngrids = ngrids
        return layout

    def _init_beams(self, primary, secondary):
        if primary is None:
            primary = BeamGaussian(
                np.radians(self.calibration.get('primbeam')))
        self.primary_beam = primary
        if secondary is None:
            secondary = BeamGaussian(
                np.radians(self.calibration.get('primbeam')), backward=True)
        self.secondary_beam = secondary

    def _init_filter(self, nu, relative_bandwidth):
        self.filter = Filter(nu, relative_bandwidth)

    def _init_horns(self):
        self.horn = self.calibration.get('hornarray')

    def _init_optics(self, polarizer):
        optics = Optics()
        calib = self.calibration.get('optics')
        optics.components = calib['components']
        optics.focal_length = calib['focal length']
        optics.polarizer = bool(polarizer)
        self.optics = optics

    def _init_synthbeam(self, dtype, synthbeam_peak150_fwhm):
        sb = SyntheticBeam()
        sb.dtype = np.dtype(dtype)
        if not self.ripples:
            sb.peak150 = BeamGaussian(synthbeam_peak150_fwhm)
        else:
            sb.peak150 = BeamGaussianRippled(synthbeam_peak150_fwhm,
                                             nripples=self.nripples)
        self.synthbeam = sb

    def __str__(self):
        state = [('ngrids', self.detector.ngrids),
                 ('selection', _compress_mask(~self.detector.all.removed)),
                 ('synthbeam_fraction', self.synthbeam.fraction),
                 ('synthbeam_peak150_fwhm_deg',
                  np.degrees(self.synthbeam.peak150.fwhm)),
                 ('synthbeam_kmax', self.synthbeam.kmax)]
        return 'Instrument:\n' + \
               '\n'.join(['    ' + a + ': ' + repr(v) for a, v in state]) + \
               '\n\nCalibration:\n' + '\n'. \
               join('    ' + l for l in str(self.calibration).splitlines())

    __repr__ = __str__

    def get_noise(self, sampling, scene, photon_noise=True, out=None,
                  operation=operation_assignment):
        """
        Return a noisy timeline.

        """
        if out is None:
            out = np.empty((len(self), len(sampling)))
        self.get_noise_detector(sampling, out=out)
        if photon_noise:
            out += self.get_noise_photon(sampling, scene)
        return out

    def get_noise_detector(self, sampling, out=None):
        """
        Return the detector noise (#det, #sampling).

        """
        return Instrument.get_noise(
            self, sampling, nep=self.detector.nep, fknee=self.detector.fknee,
            fslope=self.detector.fslope, out=out)

    def get_noise_photon(self, sampling, scene, out=None):
        """
        Return the photon noise (#det, #sampling).

        """
        nep_photon = self._get_noise_photon_nep(scene)
        return Instrument.get_noise(self, sampling, nep=nep_photon, out=out)

    def _get_noise_photon_nep(self, scene):
        """
        Return the photon noise NEP (#det,).
        """
        T_atm = scene.atmosphere.temperature
        tr_atm = scene.atmosphere.transmission
        em_atm = scene.atmosphere.emissivity
        T_cmb = scene.temperature
        cc = self.optics.components
        temperatures = np.r_[T_cmb, T_atm, cc['temperature']]
        transmissions = np.r_[1, tr_atm, cc['transmission']]
        emissivities = np.r_[1, em_atm, cc['emissivity']]
        gp = np.r_[1, 1, cc['nstates_pol']]

        n = len(temperatures)
        # tr_prod = np.cumprod(np.r_[1, transmissions[::-1]])[-2::-1]
        tr_prod = np.r_[[np.prod(transmissions[j+1:]) for j in range(n-1)], 1]

        nu = self.filter.nu
        dnu = self.filter.bandwidth
        omega_det = -self.detector.area / \
                    self.optics.focal_length**2 * \
                    np.cos(self.detector.theta)**3
        S_horn = np.pi * self.horn.radius**2 * len(self.horn)
        g = gp[:, None] * S_horn * omega_det * (nu / c)**2 * dnu
        P_phot = (emissivities * tr_prod * h * nu /
                  (np.exp(h * nu / k / temperatures) - 1))[:, None] * g
        sec_beam = self.secondary_beam(self.detector.theta,
                                       self.detector.phi)
        P_phot = P_phot * self.detector.efficiency * sec_beam
        NEP_phot_nobunch = np.sqrt(h * nu * P_phot) * np.sqrt(2)
        # note the factor sqrt(2) in the definition of the NEP
        NEP_phot = NEP_phot_nobunch * np.sqrt(1 + P_phot / (h * nu * g))
        return np.sqrt(np.sum(NEP_phot**2, 0))

    def get_aperture_integration_operator(self):
        """
        Integrate flux density in the telescope aperture.
        Convert signal from W / m^2 / Hz into W / Hz.

        """
        nhorns = np.sum(self.horn.open)
        return HomothetyOperator(nhorns * np.pi * self.horn.radius**2)

    def get_convolution_peak_operator(self, **keywords):
        """
        Return an operator that convolves the Healpix sky by the gaussian
        kernel that, if used in conjonction with the peak sampling operator,
        best approximates the synthetic beam.

        """
        if self.ripples:
            return ConvolutionRippledGaussianOperator(self.filter.nu,
                                                      **keywords)
        fwhm = self.synthbeam.peak150.fwhm * (150e9 / self.filter.nu)
        if 'ripples' in keywords.keys():
            del keywords['ripples']
        return HealpixConvolutionGaussianOperator(fwhm=fwhm, **keywords)

    def get_detector_integration_operator(self):
        """
        Integrate flux density in detector solid angles and take into account
        the secondary beam transmission.

        """
        return QubicInstrument._get_detector_integration_operator(
            self.detector.center, self.detector.area, self.secondary_beam)

    @staticmethod
    def _get_detector_integration_operator(position, area, secondary_beam):
        """
        Integrate flux density in detector solid angles and take into account
        the secondary beam transmission.

        """
        theta = np.arctan2(
            np.sqrt(np.sum(position[..., :2]**2, axis=-1)), position[..., 2])
        phi = np.arctan2(position[..., 1], position[..., 0])
        sr_det = -area / position[..., 2]**2 * np.cos(theta)**3
        sr_beam = secondary_beam.solid_angle
        sec = secondary_beam(theta, phi)
        return DiagonalOperator(sr_det / sr_beam * sec, broadcast='rightward')

    def get_detector_response_operator(self, sampling, tau=None):
        """
        Return the operator for the bolometer responses.

        """
        if tau is None:
            tau = self.detector.tau
        sampling_period = sampling.period
        shapein = len(self), len(sampling)
        if sampling_period == 0:
            return IdentityOperator(shapein)
        return ConvolutionTruncatedExponentialOperator(
            tau / sampling_period, shapein=shapein)

    def get_filter_operator(self):
        """
        Return the filter operator.
        Convert units from W/Hz to W.

        """
        if self.filter.bandwidth == 0:
            return IdentityOperator()
        return HomothetyOperator(self.filter.bandwidth)

    
    def get_hwp_operator_systemathics(self, sampling, scene):
        """
        Return the rotation matrix for the half-wave plate.

        """
        shape = (len(self), len(sampling))
        if scene.kind == 'I':
            return IdentityOperator(shapein=shape)
        if scene.kind == 'QU':
            return Rotation2dOperator(-4 * sampling.angle_hwp,
                                      degrees=True, shapein=shape + (2,))
        return Rotation3dOperator('X', -4 * sampling.angle_hwp,
                                  degrees=True, shapein=shape + (3,))

    
    def get_hwp_operator(self, sampling, scene):
        """
        Return the rotation matrix for the half-wave plate.

        """
        shape = (len(self), len(sampling))
        if scene.kind == 'I':
            return IdentityOperator(shapein=shape)
        if scene.kind == 'QU':
            return Rotation2dOperator(-4 * sampling.angle_hwp,
                                      degrees=True, shapein=shape + (2,))
        return Rotation3dOperator('X', -4 * sampling.angle_hwp,
                                  degrees=True, shapein=shape + (3,))

    def get_invntt_operator(self, sampling):
        """
        Return the inverse time-time noise correlation matrix as an Operator.

        """
        return Instrument.get_invntt_operator(
            self, sampling, fknee=self.detector.fknee,
            fslope=self.detector.fslope, ncorr=self.detector.ncorr,
            nep=self.detector.nep)

    def get_polarizer_operator(self, sampling, scene):
        """
        Return operator for the polarizer grid.
        When the polarizer is not present a transmission of 1 is assumed
        for the detectors on the first focal plane and of 0 for the other.
        Otherwise, the signal is split onto the focal planes.

        """
        nd = len(self)
        nt = len(sampling)
        grid = self.detector.quadrant // 4

        if scene.kind == 'I':
            if self.optics.polarizer:
                return HomothetyOperator(1 / 2)
            # 1 for the first detector grid and 0 for the second one
            return DiagonalOperator(1 - grid, shapein=(nd, nt),
                                    broadcast='rightward')

        if not self.optics.polarizer:
            raise NotImplementedError(
                'Polarized input is not handled without the polarizer grid.')

        z = np.zeros(nd)
        data = np.array([z + 0.5, 0.5 - grid, z]).T[:, None, None, :]
        
        #print data
        return ReshapeOperator((nd, nt, 1), (nd, nt)) * \
            DenseBlockDiagonalOperator(data, shapein=(nd, nt, 3))

    def get_projection_operator(self, sampling, scene, verbose=True):
        """
        Return the peak sampling operator.
        Convert units from W to W/sr.

        Parameters
        ----------
        sampling : QubicSampling
            The pointing information.
        scene : QubicScene
            The observed scene.
        verbose : bool, optional
            If true, display information about the memory allocation.

        """
        horn = getattr(self, 'horn', None)
        primary_beam = getattr(self, 'primary_beam', None)
        
        if sampling.fix_az:
            rotation = sampling.cartesian_horizontal2instrument
        else:
            rotation = sampling.cartesian_galactic2instrument
        
        return QubicInstrument._get_projection_operator(
            rotation, scene, self.filter.nu, self.detector.center,
            self.synthbeam, horn, primary_beam, verbose=verbose)

    @staticmethod
    def _get_projection_operator(
            rotation, scene, nu, position, synthbeam, horn, primary_beam,
            verbose=True):
        ndetectors = position.shape[0]
        ntimes = rotation.data.shape[0]
        nside = scene.nside

        thetas, phis, vals = QubicInstrument._peak_angles(
            scene, nu, position, synthbeam, horn, primary_beam)
        ncolmax = thetas.shape[-1]
        thetaphi = _pack_vector(thetas, phis)  # (ndetectors, ncolmax, 2)
        direction = Spherical2CartesianOperator('zenith,azimuth')(thetaphi)
        e_nf = direction[:, None, :, :]
        if nside > 8192:
            dtype_index = np.dtype(np.int64)
        else:
            dtype_index = np.dtype(np.int32)

        cls = {'I': FSRMatrix,
               'QU': FSRRotation2dMatrix,
               'IQU': FSRRotation3dMatrix}[scene.kind]
        ndims = len(scene.kind)
        nscene = len(scene)
        nscenetot = product(scene.shape[:scene.ndim])
        s = cls((ndetectors * ntimes * ndims, nscene * ndims), ncolmax=ncolmax,
                dtype=synthbeam.dtype, dtype_index=dtype_index,
                verbose=verbose)

        index = s.data.index.reshape((ndetectors, ntimes, ncolmax))
        c2h = Cartesian2HealpixOperator(nside)
        if nscene != nscenetot:
            table = np.full(nscenetot, -1, dtype_index)
            table[scene.index] = np.arange(len(scene), dtype=dtype_index)

        def func_thread(i):
            # e_nf[i] shape: (1, ncolmax, 3)
            # e_ni shape: (ntimes, ncolmax, 3)
            e_ni = rotation.T(e_nf[i].swapaxes(0, 1)).swapaxes(0, 1)
            if nscene != nscenetot:
                np.take(table, c2h(e_ni).astype(int), out=index[i])
            else:
                index[i] = c2h(e_ni)

        with pool_threading() as pool:
            pool.map(func_thread, xrange(ndetectors))

        if scene.kind == 'I':
            value = s.data.value.reshape(ndetectors, ntimes, ncolmax)
            value[...] = vals[:, None, :]
            shapeout = (ndetectors, ntimes)
        else:
            if str(dtype_index) not in ('int32', 'int64') or \
               str(synthbeam.dtype) not in ('float32', 'float64'):
                raise TypeError(
                    'The projection matrix cannot be created with types: {0} a'
                    'nd {1}.'.format(dtype_index, synthbeam.dtype))
            func = 'matrix_rot{0}d_i{1}_r{2}'.format(
                ndims, dtype_index.itemsize, synthbeam.dtype.itemsize)
            getattr(flib.polarization, func)(
                rotation.data.T, direction.T, s.data.ravel().view(np.int8),
                vals.T)

            if scene.kind == 'QU':
                shapeout = (ndetectors, ntimes, 2)
            else:
                shapeout = (ndetectors, ntimes, 3)
        return ProjectionOperator(s, shapeout=shapeout)

    def get_transmission_operator(self):
        """
        Return the operator that multiplies by the cumulative instrumental
        transmission.
        """
        return DiagonalOperator(
            np.product(self.optics.components['transmission']) *
            self.detector.efficiency, broadcast='rightward')

    @staticmethod
    def _peak_angles(scene, nu, position, synthbeam, horn, primary_beam):
        """
        Compute the angles and intensity of the syntheam beam peaks which
        accounts for a specified energy fraction.

        """
        theta, phi = QubicInstrument._peak_angles_kmax(
            synthbeam.kmax, horn.spacing,horn.angle, nu, position)
        val = np.array(primary_beam(theta, phi), dtype=float, copy=False)
        val[~np.isfinite(val)] = 0
        index = _argsort_reverse(val)
        theta = theta[index]
        phi = phi[index]
        val = val[index]
        cumval = np.cumsum(val, axis=-1)
        imaxs = np.argmax(cumval >= synthbeam.fraction * cumval[:, -1, None],
                          axis=-1) + 1
        imax = max(imaxs)

        # slice initial arrays to discard the non-significant peaks
        theta = theta[:, :imax]
        phi = phi[:, :imax]
        val = val[:, :imax]

        # remove additional per-detector non-significant peaks
        # and remove potential NaN in theta, phi
        for idet, imax_ in enumerate(imaxs):
            val[idet, imax_:] = 0
            theta[idet, imax_:] = np.pi / 2 #XXX 0 fails in polarization.f90.src (en2ephi and en2etheta_ephi)
            phi[idet, imax_:] = 0
        
        
        solid_angle = synthbeam.peak150.solid_angle * (150e9 / nu)**2
        val *= solid_angle / scene.solid_angle * len(horn)
        
        
        return theta, phi, val

    @staticmethod
    def _peak_angles_kmax(kmax, horn_spacing,angle, nu, position):
        """
        Return the spherical coordinates (theta, phi) of the beam peaks,
        in radians up to a maximum diffraction order.
        Parameters
        ----------
        kmax : int, optional
            The diffraction order above which the peaks are ignored.
            For instance, a value of kmax=2 will model the synthetic beam by
            (2 * kmax + 1)**2 = 25 peaks and a value of kmax=0 will only sample
            the central peak.
        horn_spacing : float
            The spacing between horns, in meters.
        nu : float
            The frequency at which the interference peaks are computed.
        position : array of shape (..., 3)
            The focal plane positions for which the angles of the interference
            peaks are computed.
        """
        lmbda = c / nu
        position = -position / np.sqrt(np.sum(position**2, axis=-1))[..., None]
        if angle !=0:
            _kx, _ky = np.mgrid[-kmax:kmax+1, -kmax:kmax+1]
            kx= _kx*np.cos(angle*np.pi/180) - _ky*np.sin(angle*np.pi/180)
            ky= _kx*np.sin(angle*np.pi/180) + _ky*np.cos(angle*np.pi/180)
        else:
            kx, ky = np.mgrid[-kmax:kmax+1, -kmax:kmax+1]
        
        nx = position[:, 0, None] - lmbda * kx.ravel() / horn_spacing
        ny = position[:, 1, None] - lmbda * ky.ravel() / horn_spacing
        local_dict = {'nx': nx, 'ny': ny}
        theta = ne.evaluate('arcsin(sqrt(nx**2 + ny**2))',
                            local_dict=local_dict)
        phi = ne.evaluate('arctan2(ny, nx)', local_dict=local_dict)
        return theta, phi

    @staticmethod
    def _get_response_A(position, area, nu, horn, secondary_beam, external_A=None):
        """
        Phase and transmission from the switches to the focal plane.

        Parameters
        ----------
        position : array-like of shape (..., 3)
            The 3D coordinates where the response is computed [m].
        area : array-like
            The integration area, in m^2.
        nu : float
            The frequency for which the response is computed [Hz].
        horn : PackedArray
            The horn layout.
        secondary_beam : Beam
            The secondary beam.
        external_A : list of tables describing the phase and amplitude at each point of the focal
            plane for each of the horns:
            [0] : array of nn with x values in meters
            [1] : array of nn with y values in meters
            [2] : array of [nhorns, nn, nn] with amplitude
            [3] : array of [nhorns, nn, nn] with phase in degrees

        Returns
        -------
        out : complex array of shape (#positions, #horns)
            The phase and transmission from the horns to the focal plane.

        """
        if external_A is None:
            uvec = position / np.sqrt(np.sum(position**2, axis=-1))[..., None]
            thetaphi = Cartesian2SphericalOperator('zenith,azimuth')(uvec)
            sr = -area / position[..., 2]**2 * np.cos(thetaphi[..., 0])**3
            tr = np.sqrt(secondary_beam(thetaphi[..., 0], thetaphi[..., 1]) *
                     sr / secondary_beam.solid_angle)[..., None]
            const = 2j * np.pi * nu / c
            product = np.dot(uvec, horn[horn.open].center.T)
            return ne.evaluate('tr * exp(const * product)')
        else:
            xx = external_A[0]
            yy =external_A[1]
            amp = external_A[2]
            phi = external_A[3]
            ix = np.argmin(np.abs(xx-position[0,0]))
            jy = np.argmin(np.abs(yy-position[0,1]))
            return np.array([amp[:,ix,jy] * (np.cos(phi[:,ix,jy]) + 1j*np.sin(phi[:,ix,jy]))])
            

    @staticmethod
    def _get_response_B(theta, phi, spectral_irradiance, nu, horn,
                        primary_beam):
        """
        Return the complex electric amplitude and phase [W^(1/2)] from sources
        of specified spectral irradiance [W/m^2/Hz] going through each horn.

        Parameters
        ----------
        theta : array-like
            The source zenith angle [rad].
        phi : array-like
            The source azimuthal angle [rad].
        spectral_irradiance : array-like
            The source spectral power per unit surface [W/m^2/Hz].
        nu : float
            The frequency for which the response is computed [Hz].
        horn : PackedArray
            The horn layout.
        primary_beam : Beam
            The primary beam.

        Returns
        -------
        out : complex array of shape (#horns, #sources)
            The phase and amplitudes from the sources to the horns.

        """
        shape = np.broadcast(theta, phi, spectral_irradiance).shape
        theta, phi, spectral_irradiance = [np.ravel(_) for _ in theta, phi,
                                           spectral_irradiance]
        uvec = hp.ang2vec(theta, phi)
        source_E = np.sqrt(spectral_irradiance *
                           primary_beam(theta, phi) * np.pi * horn.radius**2)
        const = 2j * np.pi * nu / c
        product = np.dot(horn[horn.open].center, uvec.T)
        out = ne.evaluate('source_E * exp(const * product)')
        return out.reshape((-1,) + shape)

    @staticmethod
    def _get_response(theta, phi, spectral_irradiance, position, area, nu,
                      horn, primary_beam, secondary_beam, external_A=None):
        """
        Return the monochromatic complex field [(W/Hz)^(1/2)] related to
        the electric field over a specified area of the focal plane created
        by sources of specified spectral irradiance [W/m^2/Hz]

        Parameters
        ----------
        theta : array-like
            The source zenith angle [rad].
        phi : array-like
            The source azimuthal angle [rad].
        spectral_irradiance : array-like
            The source spectral_irradiance [W/m^2/Hz].
        position : array-like of shape (..., 3)
            The 3D coordinates where the response is computed, in meters.
        area : array-like
            The integration area, in m^2.
        nu : float
            The frequency for which the response is computed [Hz].
        horn : PackedArray
            The horn layout.
        primary_beam : Beam
            The primary beam.
        secondary_beam : Beam
            The secondary beam.
        external_A : list of tables describing the phase and amplitude at each point of the focal
            plane for each of the horns:
            [0] : array of nn with x values in meters
            [1] : array of nn with y values in meters
            [2] : array of [nhorns, nn, nn] with amplitude
            [3] : array of [nhorns, nn, nn] with phase in degrees

        Returns
        -------
        out : array of shape (#positions, #sources)
            The complex field related to the electric field over a speficied
            area of the focal plane, in units of (W/Hz)^(1/2).

        """
        A = QubicInstrument._get_response_A(
                position, area, nu, horn, secondary_beam, external_A=external_A)
        B = QubicInstrument._get_response_B(
            theta, phi, spectral_irradiance, nu, horn, primary_beam)
        E = np.dot(A, B.reshape((B.shape[0], -1))).reshape(
            A.shape[:-1] + B.shape[1:])
        return E

    @staticmethod
    def _get_synthbeam(scene, position, area, nu, bandwidth, horn,
                       primary_beam, secondary_beam,
                       synthbeam_dtype=np.float32, theta_max=45, external_A=None):
        """
        Return the monochromatic synthetic beam for a specified location
        on the focal plane, multiplied by a given area and bandwidth.

        Parameters
        ----------
        scene : QubicScene
            The scene.
        position : array-like of shape (..., 3)
            The 3D coordinates where the response is computed, in meters.
        area : array-like
            The integration area, in m^2.
        nu : float
            The frequency for which the response is computed [Hz].
        bandwidth : float
            The filter bandwidth [Hz].
        horn : PackedArray
            The horn layout.
        primary_beam : Beam
            The primary beam.
        secondary_beam : Beam
            The secondary beam.
        synthbeam_dtype : dtype, optional
            The data type for the synthetic beams (default: float32).
            It is the dtype used to store the values of the pointing matrix.
        theta_max : float, optional
            The maximum zenithal angle above which the synthetic beam is
            assumed to be zero, in degrees.
        external_A : list of tables describing the phase and amplitude at each point of the focal
            plane for each of the horns:
            [0] : array of nn with x values in meters
            [1] : array of nn with y values in meters
            [2] : array of [nhorns, nn, nn] with amplitude
            [3] : array of [nhorns, nn, nn] with phase in degrees

        """
        MAX_MEMORY_B = 1e9
        theta, phi = hp.pix2ang(scene.nside, scene.index)
        index = np.where(theta <= np.radians(theta_max))[0]
        nhorn = int(np.sum(horn.open))
        npix = len(index)
        nbytes_B = npix * nhorn * 24
        ngroup = int(np.ceil(nbytes_B / MAX_MEMORY_B))
        out = np.zeros(position.shape[:-1] + (len(scene),),
                       dtype=synthbeam_dtype)
        for s in split(npix, ngroup):
            index_ = index[s]
            sb = QubicInstrument._get_response(
                theta[index_], phi[index_], bandwidth, position, area, nu,
                horn, primary_beam, secondary_beam, external_A=external_A)
            out[..., index_] = abs2(sb, dtype=synthbeam_dtype)
        return out

    def get_synthbeam(self, scene, idet=None, theta_max=45, external_A=None, detector_integrate=None, detpos=None):
        """
        Return the detector synthetic beams, computed from the superposition
        of the electromagnetic fields.

        The synthetic beam B_d = (B_d,i) of a given detector d is such that
        the power I_d in [W] collected by this detector observing a sky S=(S_i)
        in [W/m^2/Hz] is:
            I_d = (S | B_d) = sum_i S_i * B_d,i.

        Example
        -------
        >>> scene = QubicScene(1024)
        >>> inst = QubicInstrument()
        >>> sb = inst.get_synthbeam(scene, 0)

        The power collected by the bolometers in W, given a sky in W/m²/Hz is:
        >>> sb = inst.get_synthbeam(scene)
        >>> sky = scene.ones()   # [W/m²/Hz]
        >>> P = np.dot(sb, sky)  # [W]

        Parameters
        ----------
        scene : QubicScene
            The scene.
        idet : int, optional
            The detector number. By default, the synthetic beam is computed for
            all detectors.
        theta_max : float, optional
            The maximum zenithal angle above which the synthetic beam is
            assumed to be zero, in degrees.
        external_A : list of tables describing the phase and amplitude at each point of the focal
            plane for each of the horns:
            [0] : array of nn with x values in meters
            [1] : array of nn with y values in meters
            [2] : array of [nhorns, nn, nn] with amplitude
            [3] : array of [nhorns, nn, nn] with phase in degrees
        detector_integrate: Optional, number of subpixels in x direction for integration over detectors
            default (None) is no integration => uses the center of the pixel
        detpos: Optional, position in the focal plane at which the Synthesized Beam is desider as np.array([x,y,z])
        

        """
        if detpos is None:
            pos = self.detector.center
        else:
            pos = detpos

        if ((idet is not None) and (detpos is None)):
            return self[idet].get_synthbeam(scene, theta_max=theta_max, external_A=external_A,
                                            detector_integrate=detector_integrate)[0]
        if detector_integrate is None:
            return QubicInstrument._get_synthbeam(
                scene, pos, self.detector.area, self.filter.nu,
                self.filter.bandwidth, self.horn, self.primary_beam,
                self.secondary_beam, self.synthbeam.dtype, theta_max, external_A=external_A)
        else:
            xmin = np.min(self.detector.vertex[...,0:1])
            xmax = np.max(self.detector.vertex[...,0:1])
            ymin = np.min(self.detector.vertex[...,1:2])
            ymax = np.max(self.detector.vertex[...,1:2])
            allx = np.linspace(xmin, xmax, detector_integrate)
            ally = np.linspace(ymin, ymax, detector_integrate)
            sb = 0
            for i in xrange(len(allx)):
                print(i,len(allx))
                for j in xrange(len(ally)):
                    pos = self.detector.center
                    pos[0][0] = allx[i]
                    pos[0][1] = ally[j]
                    sb += QubicInstrument._get_synthbeam(
                            scene, pos, self.detector.area, self.filter.nu,
                            self.filter.bandwidth, self.horn, self.primary_beam,
                            self.secondary_beam, self.synthbeam.dtype, theta_max, external_A=external_A)/detector_integrate**2
            return sb        


def _argsort_reverse(a, axis=-1):
    i = list(np.ogrid[[slice(x) for x in a.shape]])
    i[axis] = a.argsort(axis)[:, ::-1]
    return i


def _pack_vector(*args):
    shape = np.broadcast(*args).shape
    out = np.empty(shape + (len(args),))
    for i, arg in enumerate(args):
        out[..., i] = arg
    return out

class QubicMultibandInstrument():
    """
    The QubicMultibandInstrument class
    Represents the QUBIC multiband features 
    as an array of QubicInstrumet objects
    """
    def __init__(self, d):
        '''
        filter_nus -- base frequencies array
        filter_relative_bandwidths -- array of relative bandwidths 
        center_detector -- bolean, optional
            if True, take only one detector at the centre of the focal plane
            Needed to study the synthesised beam
        '''
        
        Nf, nus_edge, filter_nus, deltas, Delta, Nbbands = self._compute_freq(d['filter_nu']/1e9, d['filter_relative_bandwidth'], d['nf_sub'])
        d1=d.copy()
        
        self.nsubbands = len(filter_nus)
        if not d['center_detector']:
            self.subinstruments = []
            for i in range(self.nsubbands):
                d1['filter_nu']= filter_nus[i]*1e9
                d1['filter_relative_bandwidth'] = deltas[i]/filter_nus[i]
                self.subinstruments +=[QubicInstrument(d1)]
        else:
                self.subinstruments = []
                for i in range(self.nsubbands):
                    d1['filter_nu']= filter_nus[i]*1e9
                    d1['filter_relative_bandwidth']= deltas[i]/filter_nus[i]
                    q = QubicInstrument(d1)[0]
                    q.detector.center = np.array([[0., 0., -0.3]])
                    self.subinstruments.append(q)

    def __getitem__(self, i):
        return self.subinstruments[i]

    def __len__(self):
        return len(self.subinstruments)
        
    def get_synthbeam(self, scene, idet=None, theta_max=45, detector_integrate=None, detpos=None):
        sb = map(lambda i: i.get_synthbeam(scene, idet, theta_max, 
                detector_integrate=detector_integrate, detpos=detpos),
                 self.subinstruments)
        sb = np.array(sb)
        bw = np.zeros(len(self))
        for i in xrange(len(self)):
            bw[i] = self[i].filter.bandwidth / 1e9
            sb[i] *= bw[i]
        sb = sb.sum(axis=0) / np.sum(bw)
        return sb

    def direct_convolution(self, scene, idet=None, theta_max=45):
        synthbeam = [q.synthbeam for q in self.subinstruments]
        for i in xrange(len(synthbeam)):
            synthbeam[i].kmax = 4
        sb_peaks = map(lambda i: QubicInstrument._peak_angles(scene, self[i].filter.nu, 
                                                        self[i][idet].detector.center, 
                                                        synthbeam[i], 
                                                        self[i].horn, 
                                                        self[i].primary_beam),
                       xrange(len(self)))
        def peaks_to_map(peaks):
            m = np.zeros(hp.nside2npix(scene.nside))
            m[hp.ang2pix(scene.nside, 
                peaks[0], 
                peaks[1])] = peaks[2]
            return m
        sb = map(peaks_to_map, sb_peaks)
        C = [i.get_convolution_peak_operator() for i in self.subinstruments]
        sb = [(C[i])(sb[i]) for i in xrange(len(self))]
        sb = np.array(sb)
        sb = sb.sum(axis=0)
        return sb


    @staticmethod
    def _compute_freq(band, relative_bandwidth=0.25, Nfreq=None):
        '''
            Prepare frequency bands parameters
            band -- int,
            QUBIC frequency band, in GHz.
            Typical values: 150, 220
            relative_bandwidth -- float, optional
            Ratio of the difference between the edges of the
            frequency band over the average frequency of the band:
            2 * (nu_max - nu_min) / (nu_max + nu_min)
            Typical value: 0.25
            Nfreq -- int, optional
            Number of frequencies within the wide band.
            If not specified, then Nfreq = 15 if band == 150
            and Nfreq = 20 if band = 220
            '''
        if Nfreq is None:
            Nfreq = {150: 15, 220: 20}[band]
        
        nu_min = band * (1 - relative_bandwidth / 2)
        nu_max = band * (1 + relative_bandwidth / 2)
        
        Nfreq_edges = Nfreq + 1
        base = (nu_max / nu_min) ** (1. / Nfreq)
        
        nus_edge = nu_min * np.logspace(0, Nfreq, Nfreq_edges, endpoint=True, base=base)
        nus = np.array([(nus_edge[i] + nus_edge[i-1]) / 2 for i in range(1, Nfreq_edges)])
        deltas = np.array([(nus_edge[i] - nus_edge[i-1])  for i in range(1, Nfreq_edges)])
        Delta = nu_max - nu_min
        Nbbands = len(nus)
        return Nfreq_edges, nus_edge, nus, deltas, Delta, Nbbands

        
