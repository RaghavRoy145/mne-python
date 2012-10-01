# Authors: Alexandre Gramfort <gramfort@nmr.mgh.harvard.edu>
#          Matti Hamalainen <msh@nmr.mgh.harvard.edu>
#          Martin Luessi <mluessi@nmr.mgh.harvard.edu>
#          Denis Engemann <d.engemann@fz-juelich.de>
#
# License: BSD (3-clause)

from math import floor, ceil
import copy
import warnings

import numpy as np
from scipy.signal import hilbert

from .constants import FIFF
from .open import fiff_open
from .meas_info import read_meas_info, write_meas_info
from .tree import dir_tree_find
from .tag import read_tag
from .pick import pick_types
from .proj import setup_proj, deactivate_proj

from ..filter import low_pass_filter, high_pass_filter, band_pass_filter
from ..parallel import parallel_func
from ..utils import deprecated, array_hash


class Raw(object):
    """Raw data

    Parameters
    ----------
    fname : string
        The name of the raw file

    allow_maxshield : bool, (default False)
        allow_maxshield if True, allow loading of data that has been
        processed with Maxshield. Maxshield-processed data should generally
        not be loaded directly, but should be processed using SSS first.

    preload : bool or str (default False)
        Preload data into memory for data manipulation and faster indexing.
        If True, the data will be preloaded into memory (fast, requires
        large amount of memory). If preload is a string, preload is the
        file name of a memory-mapped file which is used to store the data
        on the hard drive (slower, requires less memory).

    verbose : bool
        Use verbose output

    proj : bool
        If True, set self.proj to true. With preload=True, this will cause
        the projectors to be applied when loading the data.

    Attributes
    ----------
    info: dict
        Measurement info

    ch_names: list of string
        List of channels' names

    verbose : bool
        Use verbose output.

    preload : bool
        Are data preloaded from disk?

    proj : bool
        Apply or not the SSPs projections taken from info['projs']
        when accessing data.
    """
    def __init__(self, fname, allow_maxshield=False, preload=False,
                 verbose=True, proj=False):
        #   Open the file
        if verbose:
            print 'Opening raw data file %s...' % fname
        fid, tree, _ = fiff_open(fname)

        #   Read the measurement info
        info, meas = read_meas_info(fid, tree)

        #   Locate the data of interest
        raw_node = dir_tree_find(meas, FIFF.FIFFB_RAW_DATA)
        if len(raw_node) == 0:
            raw_node = dir_tree_find(meas, FIFF.FIFFB_CONTINUOUS_DATA)
            if allow_maxshield:
                raw_node = dir_tree_find(meas, FIFF.FIFFB_SMSH_RAW_DATA)
                if len(raw_node) == 0:
                    raise ValueError('No raw data in %s' % fname)
            else:
                if len(raw_node) == 0:
                    raise ValueError('No raw data in %s' % fname)

        if len(raw_node) == 1:
            raw_node = raw_node[0]

        #   Set up the output structure
        info['filename'] = fname

        #   Process the directory
        directory = raw_node['directory']
        nent = raw_node['nent']
        nchan = int(info['nchan'])
        first = 0
        first_samp = 0
        first_skip = 0

        #   Get first sample tag if it is there
        if directory[first].kind == FIFF.FIFF_FIRST_SAMPLE:
            tag = read_tag(fid, directory[first].pos)
            first_samp = int(tag.data)
            first += 1

        #   Omit initial skip
        if directory[first].kind == FIFF.FIFF_DATA_SKIP:
            # This first skip can be applied only after we know the buffer size
            tag = read_tag(fid, directory[first].pos)
            first_skip = int(tag.data)
            first += 1

        self.first_samp = first_samp

        #   Go through the remaining tags in the directory
        rawdir = list()
        nskip = 0
        for k in range(first, nent):
            ent = directory[k]
            if ent.kind == FIFF.FIFF_DATA_SKIP:
                tag = read_tag(fid, ent.pos)
                nskip = int(tag.data)
            elif ent.kind == FIFF.FIFF_DATA_BUFFER:
                #   Figure out the number of samples in this buffer
                if ent.type == FIFF.FIFFT_DAU_PACK16:
                    nsamp = ent.size / (2 * nchan)
                elif ent.type == FIFF.FIFFT_SHORT:
                    nsamp = ent.size / (2 * nchan)
                elif ent.type == FIFF.FIFFT_FLOAT:
                    nsamp = ent.size / (4 * nchan)
                elif ent.type == FIFF.FIFFT_INT:
                    nsamp = ent.size / (4 * nchan)
                elif ent.type == FIFF.FIFFT_COMPLEX_FLOAT:
                    nsamp = ent.size / (8 * nchan)
                else:
                    fid.close()
                    raise ValueError('Cannot handle data buffers of type %d' %
                                                                      ent.type)

                #  Do we have an initial skip pending?
                if first_skip > 0:
                    first_samp += nsamp * first_skip
                    self.first_samp = first_samp
                    first_skip = 0

                #  Do we have a skip pending?
                if nskip > 0:
                    rawdir.append(dict(ent=None, first=first_samp,
                                       last=first_samp + nskip * nsamp - 1,
                                       nsamp=nskip * nsamp))
                    first_samp += nskip * nsamp
                    nskip = 0

                #  Add a data buffer
                rawdir.append(dict(ent=ent, first=first_samp,
                                   last=first_samp + nsamp - 1,
                                   nsamp=nsamp))
                first_samp += nsamp

        self.last_samp = first_samp - 1

        #   Add the calibration factors
        cals = np.zeros(info['nchan'])
        for k in range(info['nchan']):
            cals[k] = info['chs'][k]['range'] * \
                      info['chs'][k]['cal']

        self.cals = cals
        self.rawdir = rawdir
        self.comp = None
        # XXX self.comp never changes!
        if verbose:
            print '    Range : %d ... %d =  %9.3f ... %9.3f secs' % (
                       self.first_samp, self.last_samp,
                       float(self.first_samp) / info['sfreq'],
                       float(self.last_samp) / info['sfreq'])
            print 'Ready.'

        self.fid = fid
        self.info = info
        self.verbose = verbose
        self.proj = proj
        self._projector, self.info = setup_proj(self.info)
        self._projector_hash = _hash_projs(self.info['projs'], self._projector)
        self._preloaded = False
        if preload:
            self.reload(init=preload)

    def _parse_get_set_params(self, item):
        # make sure item is a tuple
        if not isinstance(item, tuple):  # only channel selection passed
            item = (item, slice(None, None, None))

        if len(item) != 2:  # should be channels and time instants
            raise RuntimeError("Unable to access raw data (need both channels "
                               "and time)")

        time_slice = item[1]
        if isinstance(item[0], slice):
            start = item[0].start if item[0].start is not None else 0
            nchan = self.info['nchan']
            stop = item[0].stop if item[0].stop is not None else nchan
            step = item[0].step if item[0].step is not None else 1
            sel = range(start, stop, step)
        else:
            sel = item[0]

        start, stop, step = time_slice.start, time_slice.stop, \
                            time_slice.step
        if start is None:
            start = 0
        if step is not None:
            raise ValueError('step needs to be 1 : %d given' % step)

        if isinstance(sel, int):
            sel = np.array([sel])

        if sel is not None and len(sel) == 0:
            raise ValueError("Empty channel list")

        return sel, start, stop

    def __getitem__(self, item):
        """getting raw data content with python slicing"""
        sel, start, stop = self._parse_get_set_params(item)
        if self._preloaded:
            data, times = self._data[sel, start:stop], self._times[start:stop]
            was_updated = _update_projector(self)
            if was_updated:
                raise RuntimeError('Changing projector after preloading data'
                                   'is not allowed')
        else:
            data, times = read_raw_segment(self, start=start, stop=stop,
                                           sel=sel, verbose=self.verbose)
        return data, times

    def __setitem__(self, item, value):
        """setting raw data content with python slicing"""
        if not self._preloaded:
            raise RuntimeError('Modifying data of Raw is only supported '
                               'when preloading is used. Use preload=True '
                               '(or string) in the constructor.')
        sel, start, stop = self._parse_get_set_params(item)
        # set the data
        self._data[sel, start:stop] = value

    def apply_function(self, fun, picks, dtype, n_jobs, verbose, *args,
                       **kwargs):
        """ Apply a function to a subset of channels.

        The function "fun" is applied to the channels defined in "picks". The
        data of the Raw object is modified inplace. If the function returns
        a different data type (e.g. numpy.complex) it must be specified using
        the dtype parameter, which causes the data type used for representing
        the raw data to change.

        The Raw object has to be constructed using preload=True (or string).

        Note: If n_jobs > 1, more memory is required as "len(picks) * n_times"
              addtional time points need to be temporaily stored in memory.

        Note: If the data type changes (dtype != None), more memory is required
              since the original and the converted data needs to be stored in
              memory.

        Parameters
        ----------
        fun : function
            A function to be applied to the channels. The first argument of
            fun has to be a timeseries (numpy.ndarray). The function must
            return an numpy.ndarray with the same size as the input.

        picks : list of int
            Indices of channels to apply the function to.

        dtype : numpy.dtype
            Data type to use for raw data after applying the function. If None
            the data type is not modified.

        n_jobs: int
            Number of jobs to run in parallel.

        verbose: int
            Verbosity level.

        *args:
            Additional positional arguments to pass to fun (first pos. argument
            of fun is the timeseries of a channel).

        **kwargs:
            Keyword arguments to pass to fun.
        """
        if not self._preloaded:
            raise RuntimeError('Raw data needs to be preloaded. Use '
                               'preload=True (or string) in the constructor.')

        if not callable(fun):
            raise ValueError('fun needs to be a function')

        data_in = self._data
        if dtype is not None and dtype != self._data.dtype:
            self._data = self._data.astype(dtype)

        if n_jobs == 1:
            # modify data inplace to save memory
            for idx in picks:
                self._data[idx, :] = fun(data_in[idx, :], *args, **kwargs)
        else:
            # use parallel function
            parallel, p_fun, _ = parallel_func(fun, n_jobs, verbose)

            data_picks = data_in[picks, :]
            data_picks_new = np.array(parallel(p_fun(x, *args, **kwargs)
                                      for x in data_picks))

            self._data[picks, :] = data_picks_new

    def apply_hilbert(self, picks, envelope=False, n_jobs=1, verbose=5):
        """ Compute analytic signal or envelope for a subset of channels.

        If envelope=False, the analytic signal for the channels defined in
        "picks" is computed and the data of the Raw object is converted to
        a complex representation (the analytic signal is complex valued).

        If envelope=True, the absolute value of the analytic signal for the
        channels defined in "picks" is computed, resulting in the envelope
        signal.

        Note: DO NOT use envelope=True if you intend to compute an inverse
              solution from the raw data. If you want to compute the
              envelope in source space, use envelope=False and compute the
              envelope after the inverse solution has been obtained.

        Note: If envelope=False, more memory is required since the original
              raw data as well as the analytic signal have temporarily to
              be stored in memory.

        Note: If n_jobs > 1 and envelope=True, more memory is required as
              "len(picks) * n_times" addtional time points need to be
              temporaily stored in memory.

        Parameters
        ----------
        picks : list of int
            Indices of channels to apply the function to.

        envelope : bool (default: False)
            Compute the envelope signal of each channel.

        n_jobs: int
            Number of jobs to run in parallel.

        verbose: int
            Verbosity level.

        Notes
        -----
        The analytic signal "x_a(t)" of "x(t)" is::

            x_a = F^{-1}(F(x) 2U) = x + i y

        where "F" is the Fourier transform, "U" the unit step function,
        and "y" the Hilbert transform of "x". One usage of the analytic
        signal is the computation of the envelope signal, which is given by
        "e(t) = abs(x_a(t))". Due to the linearity of Hilbert transform and the
        MNE inverse solution, the enevlope in source space can be obtained
        by computing the analytic signal in sensor space, applying the MNE
        inverse, and computing the envelope in source space.
        """
        if envelope:
            self.apply_function(_envelope, picks, None, n_jobs, verbose)
        else:
            self.apply_function(hilbert, picks, np.complex64, n_jobs, verbose)

    def filter(self, l_freq, h_freq, picks=None, filter_length=None,
               l_trans_bandwidth=0.5, h_trans_bandwidth=0.5, n_jobs=1,
               verbose=5):
        """Filter a subset of channels.

        Applies a zero-phase band-pass filter to the channels selected by
        "picks". The data of the Raw object is modified inplace.

        The Raw object has to be constructed using preload=True (or string).

        Note: If n_jobs > 1, more memory is required as "len(picks) * n_times"
              addtional time points need to be temporaily stored in memory.

        Parameters
        ----------
        l_freq : float | None
            Low cut-off frequency in Hz. If None the data are only low-passed.

        h_freq : float
            High cut-off frequency in Hz. If None the data are only
            high-passed.

        picks : list of int | None
            Indices of channels to filter. If None only the data (MEG/EEG)
            channels will be filtered.

        filter_length : int (default: None)
            Length of the filter to use (e.g. 4096).
            If None or "n_times < filter_length",
            (n_times: number of timepoints in Raw object) the filter length
            used is n_times. Otherwise, overlap-add filtering with a
            filter of the specified length is used (faster for long signals).
        l_trans_bandwidth : float
            Width of the transition band at the low cut-off frequency in Hz.
        h_trans_bandwidth : float
            Width of the transition band at the high cut-off frequency in Hz.
        n_jobs: int (default: 1)
            Number of jobs to run in parallel.
        verbose: int (default: 5)
            Verbosity level.
        """
        fs = float(self.info['sfreq'])
        if l_freq == 0:
            l_freq = None
        if h_freq > (fs / 2.):
            h_freq = None
        if picks is None:
            picks = pick_types(self.info, meg=True, eeg=True)
        if l_freq is None and h_freq is not None:
            self.apply_function(low_pass_filter, picks, None, n_jobs, verbose,
                                fs, h_freq, filter_length=filter_length,
                                trans_bandwidth=l_trans_bandwidth)
        if l_freq is not None and h_freq is None:
            self.apply_function(high_pass_filter, picks, None, n_jobs, verbose,
                                fs, l_freq, filter_length=filter_length,
                                trans_bandwidth=h_trans_bandwidth)
        if l_freq is not None and h_freq is not None:
            self.apply_function(band_pass_filter, picks, None, n_jobs, verbose,
                                fs, l_freq, h_freq,
                                filter_length=filter_length,
                                l_trans_bandwidth=l_trans_bandwidth,
                                h_trans_bandwidth=h_trans_bandwidth)

    def reload(self, init=False):
        """Reload raw data from disk.

        This will reload all the data from disk. If self.proj=True, projection
        and compensation will be applied.

        Parameters
        ----------
        init : bool or str (default False)
            Initialization parameter. If True, the data will be preloaded into
            memory (fast, requires large amount of memory). If preload is a
            string, preload is the file name of a memory-mapped file which is
            used to store the data on the hard drive (slower, requires less
            memory). init=False is the same as init=True, but will throw a
            warning if the data has not previously been preloaded.
        """
        if (self._preloaded is not True) and not init:
            warnings.warn('Data was notpreviously preloaded, preloading now')
            init = True
        if init:
            nchan = self.info['nchan']
            nsamp = self.last_samp - self.first_samp + 1
            if isinstance(init, str):
                # preload data using a memmap file
                self._data = np.memmap(init, mode='w+', dtype='float32',
                                       shape=(nchan, nsamp))
            else:
                self._data = np.empty((nchan, nsamp), dtype='float32')
        self._data, self._times = read_raw_segment(self,
                                                   data_buffer=self._data)
        self._preloaded = True

    def apply_projector(self):
        """Apply projection vectors

        When data are preloaded is directly applied or they are set be
        applied to data as it is read from disk.
        """
        self.proj = True
        _update_projector(self)
        if self._preloaded:
            self._data = np.dot(self._projector, self._data)

    @deprecated('band_pass_filter is deprecated please use raw.filter instead')
    def band_pass_filter(self, picks, l_freq, h_freq, filter_length=None,
                         n_jobs=1, verbose=5):
        """Band-pass filter a subset of channels.

        Applies a zero-phase band-pass filter to the channels selected by
        "picks". The data of the Raw object is modified inplace.

        The Raw object has to be constructed using preload=True (or string).

        Note: If n_jobs > 1, more memory is required as "len(picks) * n_times"
              addtional time points need to be temporaily stored in memory.

        Parameters
        ----------
        picks : list of int
            Indices of channels to filter.

        l_freq : float
            Low cut-off frequency in Hz.

        h_freq : float
            High cut-off frequency in Hz.

        filter_length : int (default: None)
            Length of the filter to use. If None or "n_times < filter_length",
            (n_times: number of timepoints in Raw object) the filter length
            used is n_times. Otherwise, overlap-add filtering with a
            filter of the specified length is used (faster for long signals).

        n_jobs: int (default: 1)
            Number of jobs to run in parallel.

        verbose: int (default: 5)
            Verbosity level.
        """
        self.filter(l_freq, h_freq, picks, n_jobs=n_jobs, verbose=verbose,
                    filter_length=filter_length)

    @deprecated('high_pass_filter is deprecated please use raw.filter instead')
    def high_pass_filter(self, picks, freq, filter_length=None, n_jobs=1,
                         verbose=5):
        """High-pass filter a subset of channels.

        Applies a zero-phase high-pass filter to the channels selected by
        "picks". The data of the Raw object is modified inplace.

        Note: If n_jobs > 1, more memory is required as "len(picks) * n_times"
              addtional time points need to be temporaily stored in memory.

        The Raw object has to be constructed using preload=True (or string).

        Parameters
        ----------
        picks : list of int
            Indices of channels to filter.

        freq : float
            Cut-off frequency in Hz.

        filter_length : int (default: None)
            Length of the filter to use. If None or "n_times < filter_length",
            (n_times: number of timepoints in Raw object) the filter length
            used is n_times. Otherwise, overlap-add filtering with a
            filter of the specified length is used (faster for long signals).

        n_jobs: int (default: 1)
            Number of jobs to run in parallel.

        verbose: int (default: 5)
            Verbosity level.
        """
        self.filter(freq, None, picks, n_jobs=n_jobs, verbose=verbose,
                    filter_length=filter_length)

    @deprecated('low_pass_filter is deprecated please use raw.filter instead')
    def low_pass_filter(self, picks, freq, filter_length=None, n_jobs=1,
                        verbose=5):
        """Low-pass filter a subset of channels.

        Applies a zero-phase low-pass filter to the channels selected by
        "picks". The data of the Raw object is modified in-place.

        Note: If n_jobs > 1, more memory is required as "len(picks) * n_times"
              addtional time points need to be temporaily stored in memory.

        The Raw object has to be constructed using preload=True (or string).

        Parameters
        ----------
        picks : list of int
            Indices of channels to filter.

        freq : float
            Cut-off frequency in Hz.

        filter_length : int (default: None)
            Length of the filter to use. If None or "n_times < filter_length",
            (n_times: number of timepoints in Raw object) the filter length
            used is n_times. Otherwise, overlap-add filtering with a
            filter of the specified length is used (faster for long signals).

        n_jobs: int (default: 1)
            Number of jobs to run in parallel.

        verbose: int (default: 5)
            Verbosity level.
        """
        self.filter(None, freq, picks, n_jobs=n_jobs, verbose=verbose,
                    filter_length=filter_length)

    def add_proj(self, projs, remove_existing=False):
        """Add SSP projection vectors

        If Raw was created asking for projections to be applied
        the projection matrix gets updated.

        Parameters
        ----------
        projs : list
            List with projection vectors

        remove_existing : bool
            Remove the projection vectors currently in the file
        """
        projs = copy.deepcopy(projs)

        if remove_existing:
            self.info['projs'] = projs
        else:
            self.info['projs'].extend(projs)
        _update_projector(self)

    def save(self, fname, picks=None, tmin=0, tmax=None, buffer_size_sec=10,
             drop_small_buffer=False, proj_active=None):
        """Save raw data to file

        Parameters
        ----------
        fname : string
            File name of the new dataset. Caveat! This has to be a new
            filename.

        picks : list of int
            Indices of channels to include

        tmin : float
            Time in seconds of first sample to save

        tmax : int
            Time in seconds of last sample to save

        buffer_size_sec : float
            Size of data chuncks in seconds.

        drop_small_buffer: bool
            Drop or not the last buffer. It is required by maxfilter (SSS)
            that only accepts raw files with buffers of the same size.

        proj_active: bool or None
            If True/False, the data is saved with the projections set to
            active/inactive. If None, True/False is inferred from self.proj.

        """
        if fname == self.info['filename']:
            raise ValueError('You cannot save data to the same file.'
                               ' Please use a different filename.')

        if self._preloaded:
            if np.iscomplexobj(self._data):
                warnings.warn('Saving raw file with complex data. Loading '
                              'with command-line MNE tools will not work.')

        # if proj is off, deactivate projs so data isn't saved with them on
        # don't have to worry about activating them because they default to on
        if proj_active is None:
            proj_active = self.proj
        if not proj_active:
            self.info['projs'] = deactivate_proj(self.info['projs'])

        outfid, cals = start_writing_raw(fname, self.info, picks)
        #
        #   Set up the reading parameters
        #

        #   Convert to samples
        start = int(floor(tmin * self.info['sfreq']))
        first_samp = self.first_samp + start

        if tmax is None:
            stop = self.last_samp + 1 - self.first_samp
        else:
            stop = int(floor(tmax * self.info['sfreq']))

        buffer_size = int(ceil(buffer_size_sec * self.info['sfreq']))
        #
        #   Read and write all the data
        #
        write_int(outfid, FIFF.FIFF_FIRST_SAMPLE, first_samp)
        for first in range(start, stop, buffer_size):
            last = first + buffer_size
            if last >= stop:
                last = stop + 1

            if picks is None:
                data, times = self[:, first:last]
            else:
                data, times = self[picks, first:last]

            if (drop_small_buffer and (first > start)
                                            and (len(times) < buffer_size)):
                print 'Skipping data chunk due to small buffer ... [done]\n'
                break

            print 'Writing ... ',
            write_raw_buffer(outfid, data, cals)
            print '[done]'

        finish_writing_raw(outfid)

    def time_to_index(self, *args):
        indices = []
        for time in args:
            ind = int(time * self.info['sfreq'])
            indices.append(ind)
        return indices

    @property
    def ch_names(self):
        return self.info['ch_names']

    def close(self):
        self.fid.close()

    def __repr__(self):
        s = "n_channels x n_times : %s x %s" % (len(self.info['ch_names']),
                                       self.last_samp - self.first_samp + 1)
        return "Raw (%s)" % s


def _update_projector(raw):
    """Update hash new projector variables and
    update .projector if it is necessary
    """
    new_hash = _hash_projs(raw.info['projs'], raw._projector)
    if not new_hash == raw._projector_hash:
        raw._projector, raw.info = setup_proj(raw.info)
        raw._projector_hash = _hash_projs(raw.info['projs'], raw._projector)
        return True
    else:
        return False


def _hash_projs(projs, projector):
    out_hash = [array_hash(p['data']['data']) for p in projs]
    if projector is not None:
        out_hash.append(array_hash(projector))
    return out_hash


def read_raw_segment(raw, start=0, stop=None, sel=None, data_buffer=None,
    verbose=False):
    """Read a chunck of raw data

    Parameters
    ----------
    raw: Raw object
        An instance of Raw

    start: int, (optional)
        first sample to include (first is 0). If omitted, defaults to the first
        sample in data

    stop: int, (optional)
        First sample to not include.
        If omitted, data is included to the end.

    sel: array, optional
        Indices of channels to select

    data_buffer: array, optional
        numpy array to fill with data read, must have the correct shape

    verbose: bool
        Use verbose output

    Returns
    -------
    data: array, [channels x samples]
       the data matrix (channels x samples)

    times: array, [samples]
        returns the time values corresponding to the samples
    """
    if stop is None:
        stop = raw.last_samp + 1

    #  Initial checks
    start = int(start + raw.first_samp)
    stop = int(stop + raw.first_samp)

    if stop >= raw.last_samp:
        stop = raw.last_samp + 1

    if start >= stop:
        raise ValueError('No data in this range')

    if verbose:
        print 'Reading %d ... %d  =  %9.3f ... %9.3f secs...' % (
                           start, stop - 1, start / float(raw.info['sfreq']),
                           (stop - 1) / float(raw.info['sfreq'])),

    #  Initialize the data and calibration vector
    nchan = raw.info['nchan']
    dest = 0

    n_sel_channels = nchan if sel is None else len(sel)
    idx = slice(None, None, None) if sel is None else sel
    data_shape = (n_sel_channels, stop - start)
    if data_buffer is not None:
        if data_buffer.shape != data_shape:
            raise ValueError('data_buffer has incorrect shape')
        data = data_buffer
    else:
        data = None  # we will allocate it later, once we know the type

    _update_projector(raw)
    if raw.proj:
        mult = np.diag(raw.cals.ravel())
        if raw.comp is not None:
            mult = np.dot(raw.comp[idx, :], mult)
        if raw._projector is not None:
            mult = np.dot(raw._projector, mult)
    else:
        mult = None

    do_debug = False
    # do_debug = True

    for this in raw.rawdir:

        #  Do we need this buffer
        if this['last'] >= start:
            if this['ent'] is None:
                #  Take the easy route: skip is translated to zeros
                if do_debug:
                    print 'S'
                one = np.zeros((n_sel_channels, this['nsamp']))
            else:
                tag = read_tag(raw.fid, this['ent'].pos)

                # decide what datatype to use
                if np.isrealobj(tag.data):
                    dtype = np.float
                else:
                    dtype = np.complex64

                one = tag.data.reshape(this['nsamp'], nchan).astype(dtype).T
                if mult is not None:  # use proj + calibration factors in mult
                    one = np.dot(mult, one)
                    one = one[idx]
                else:  # apply just the calibration factors
                    one = raw.cals.ravel()[idx][:, np.newaxis] * one[idx]

            #  The picking logic is a bit complicated
            if stop - 1 > this['last'] and start < this['first']:
                #    We need the whole buffer
                first_pick = 0
                last_pick = this['nsamp']
                if do_debug:
                    print 'W'

            elif start >= this['first']:
                first_pick = start - this['first']
                if stop - 1 <= this['last']:
                    #   Something from the middle
                    last_pick = this['nsamp'] + stop - this['last'] - 1
                    if do_debug:
                        print 'M'
                else:
                    #   From the middle to the end
                    last_pick = this['nsamp']
                    if do_debug:
                        print 'E'
            else:
                #    From the beginning to the middle
                first_pick = 0
                last_pick = stop - this['first']
                if do_debug:
                    print 'B'

            #   Now we are ready to pick
            picksamp = last_pick - first_pick
            if picksamp > 0:
                if data is None:
                    # if not already done, allocate array with right type
                    data = np.empty(data_shape, dtype=dtype)
                data[:, dest:(dest + picksamp)] = one[:, first_pick:last_pick]
                dest += picksamp

        #   Done?
        if this['last'] >= stop - 1:
            if verbose:
                print ' [done]'
            break

    times = (np.arange(start, stop) - raw.first_samp) / raw.info['sfreq']

    raw.fid.seek(0, 0)  # Go back to beginning of the file

    return data, times


def read_raw_segment_times(raw, start, stop, sel=None, verbose=True):
    """Read a chunck of raw data

    Parameters
    ----------
    raw: Raw object
        An instance of Raw

    start: float
        Starting time of the segment in seconds

    stop: float
        End time of the segment in seconds

    sel: array, optional
        Indices of channels to select

    node: tree node
        The node of the tree where to look

    verbose: bool
        Use verbose output

    Returns
    -------
    data: array, [channels x samples]
       the data matrix (channels x samples)

    times: array, [samples]
        returns the time values corresponding to the samples
    """
    #   Convert to samples
    start = floor(start * raw.info['sfreq'])
    stop = ceil(stop * raw.info['sfreq'])

    #   Read it
    return read_raw_segment(raw, start, stop, sel, verbose=verbose)

###############################################################################
# Writing

from .write import start_file, end_file, start_block, end_block, \
                   write_float, write_complex64, write_int, write_id


def start_writing_raw(name, info, sel=None):
    """Start write raw data in file

    Data will be written in float

    Parameters
    ----------
    name : string
        Name of the file to create.

    info : dict
        Measurement info

    sel : array of int, optional
        Indices of channels to include. By default all channels are included.

    Returns
    -------
    fid : file
        The file descriptor

    cals : list
        calibration factors
    """
    #
    #  Create the file and save the essentials
    #
    fid = start_file(name)
    start_block(fid, FIFF.FIFFB_MEAS)
    write_id(fid, FIFF.FIFF_BLOCK_ID)
    if info['meas_id'] is not None:
        write_id(fid, FIFF.FIFF_PARENT_BLOCK_ID, info['meas_id'])
    #
    #    Measurement info
    #
    if sel is not None:
        info = copy.deepcopy(info)
        info['chs'] = [info['chs'][k] for k in sel]
        info['nchan'] = len(sel)

        ch_names = [c['ch_name'] for c in info['chs']]  # name of good channels
        comps = copy.deepcopy(info['comps'])
        for c in comps:
            row_idx = [k for k, n in enumerate(c['data']['row_names'])
                                                            if n in ch_names]
            row_names = [c['data']['row_names'][i] for i in row_idx]
            rowcals = c['rowcals'][row_idx]
            c['rowcals'] = rowcals
            c['data']['nrow'] = len(row_names)
            c['data']['row_names'] = row_names
            c['data']['data'] = c['data']['data'][row_idx]
        info['comps'] = comps

    cals = []
    for k in range(info['nchan']):
        #
        #   Scan numbers may have been messed up
        #
        info['chs'][k]['scanno'] = k + 1  # scanno starts at 1 in FIF format
        info['chs'][k]['range'] = 1.0
        cals.append(info['chs'][k]['cal'])

    write_meas_info(fid, info, data_type=4)

    #
    # Start the raw data
    #
    start_block(fid, FIFF.FIFFB_RAW_DATA)

    return fid, cals


def write_raw_buffer(fid, buf, cals):
    """Write raw buffer

    Parameters
    ----------
    fid : file descriptor
        an open raw data file

    buf : array
        The buffer to write

    cals : array
        Calibration factors
    """
    if buf.shape[0] != len(cals):
        raise ValueError('buffer and calibration sizes do not match')

    if np.isrealobj(buf):
        write_float(fid, FIFF.FIFF_DATA_BUFFER, buf / np.ravel(cals)[:, None])
    else:
        write_complex64(fid, FIFF.FIFF_DATA_BUFFER,
                        buf / np.ravel(cals)[:, None])


def finish_writing_raw(fid):
    """Finish writing raw FIF file

    Parameters
    ----------
    fid : file descriptor
        an open raw data file
    """
    end_block(fid, FIFF.FIFFB_RAW_DATA)
    end_block(fid, FIFF.FIFFB_MEAS)
    end_file(fid)


def _envelope(x):
    """ Compute envelope signal """
    return np.abs(hilbert(x))
