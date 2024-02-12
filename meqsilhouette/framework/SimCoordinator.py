from Pyxis.ModSupport import *
from meqsilhouette.framework.meqtrees_funcs import run_turbosim, run_wsclean, copy_between_cols
import pyrap.tables as pt
import pyrap.measures as pm, pyrap.quanta as qa
from meqsilhouette.framework.comm_functions import *
import pickle
import subprocess
import os
import ast
import time
import glob
import shlex
import tempfile
import cmath
import numpy as np
from scipy.constants import Boltzmann, speed_of_light
from scipy.interpolate import InterpolatedUnivariateSpline as ius
import pylab as pl
import seaborn as sns
#sns.set_style("darkgrid")
cmap = pl.cm.Set1 #tab20 #viridis
from matplotlib import rc
rc('font',**{'family':'serif','serif':['Times']})
rc('text', usetex=True)
FSIZE=24
from mpltools import layout
from mpltools import color
from matplotlib.patches import Circle
from matplotlib.ticker import FormatStrFormatter
from cycler import cycler


class SimCoordinator():

    def __init__(self, msname, output_column, input_fitsimage, input_fitspol, input_changroups, bandpass_table, bandpass_freq_interp_order, sefd, \
                 corr_eff, predict_oversampling, predict_seed, atm_seed, aperture_eff, elevation_limit, trop_enabled, trop_wetonly, pwv, \
                 gpress, gtemp, coherence_time, fixdelay_max_picosec, uvjones_g_on, uvjones_d_on, parang_corrected, gR_mean, \
                 gR_std, gL_mean, gL_std, dR_mean, dR_std, dL_mean, dL_std, feed_angle, thermal_noise_enabled):
        info('Generating MS attributes based on input parameters')
        self.msname = msname
        tab = pt.table(msname, readonly=True,ack=False)
        self.data = tab.getcol(output_column)
        self.flag = tab.getcol('FLAG')
        self.uvw = tab.getcol("UVW")
        self.uvdist = np.sqrt(self.uvw[:, 0]**2 + self.uvw[:, 1]**2)
        self.A0 = tab.getcol('ANTENNA1')
        self.A1 = tab.getcol("ANTENNA2")
        self.time = tab.getcol('TIME')
        self.nrows = self.time.shape[0]
        self.chunksize = 100000 # hardcoded for now
        self.nchunks = int(np.ceil(float(self.nrows)/self.chunksize))
        self.time_unique = np.unique(self.time)
        self.mjd_obs_start = self.time_unique[0]
        self.mjd_obs_end  = self.time_unique[-1]
        self.tint = self.time_unique[1]-self.time_unique[0]
        self.obslength = self.time_unique[-1]-self.time_unique[0]
        self.ant_unique = np.unique(np.hstack((self.A0, self.A1)))


        anttab = pt.table(tab.getkeyword('ANTENNA'),ack=False)
        self.station_names = anttab.getcol('NAME')
        self.pos = anttab.getcol('POSITION')
        self.mount = anttab.getcol('MOUNT')
        self.Nant = self.pos.shape[0]
        self.N = range(self.Nant)
        self.nbl = (self.Nant*(self.Nant-1))/2
        anttab.close()

        field_tab = pt.table(tab.getkeyword('FIELD'),ack=False)
        self.direction = np.squeeze(field_tab.getcol('PHASE_DIR'))

        spec_tab = pt.table(tab.getkeyword('SPECTRAL_WINDOW'),ack=False)
        self.num_chan = spec_tab.getcol('NUM_CHAN')[0]
        self.chan_freq = spec_tab.getcol('CHAN_FREQ').flatten()
        self.chan_width = spec_tab.getcol('CHAN_WIDTH').flatten()[0]
        self.bandwidth = self.chan_width + self.chan_freq[-1]-self.chan_freq[0]
        spec_tab.close()

        ### elevation-relevant calculation ###
        self.elevation = self.elevation_calc()
        self.baseline_dict = self.make_baseline_dictionary()
        self.write_flag(elevation_limit)
        self.elevation_copy_dterms = self.elevation.copy()
        self.elevation[self.elevation < elevation_limit] = np.nan  # This is to avoid crashing later tropospheric calculation
        self.calc_ant_rise_set_times()
        self.parallactic_angle = self.parallactic_angle_calc() # INI: uses self.elevation
                                                
        self.input_fitsimage = input_fitsimage
        self.input_fitspol = input_fitspol
        self.input_changroups = input_changroups
        self.output_column = output_column

        ### thermal noise relevant calculations ###
        self.SEFD = sefd
        self.dish_diameter = pt.table(pt.table(msname).getkeyword('ANTENNA'),ack=False).getcol('DISH_DIAMETER')
        if np.any(self.dish_diameter == 0):
            abort("One of the dish diameters in the ANTENNA table is zero. Aborting execution.")
        self.dish_area = aperture_eff * np.pi * np.power((self.dish_diameter / 2.), 2)
        self.receiver_temp = (self.SEFD * self.dish_area / (2 * Boltzmann)) / 1e26 # not used, but compare with real values
        self.corr_eff = corr_eff

        ### INI: Oversampling factor to use for visibility prediction
        self.oversampling = predict_oversampling
        if predict_seed != -1:
            self.rng_predict = np.random.default_rng(predict_seed)
        else:
            self.rng_predict = np.random.default_rng()
        if atm_seed != -1:
            self.rng_atm = np.random.default_rng(atm_seed)
        else:
            self.rng_atm = np.random.default_rng()

        ### INI: populate WEIGHT and SIGMA columns
        self.thermal_noise_enabled = thermal_noise_enabled
        self.receiver_rms = np.zeros(self.data.shape, dtype='float')

        tab.close() # close main MS table

        ### troposphere information
        self.trop_enabled = trop_enabled
        self.trop_wetonly = trop_wetonly
        self.average_pwv = pwv
        self.average_gpress = gpress
        self.average_gtemp = gtemp
        self.coherence_time = coherence_time
        self.fixdelay_max_picosec = fixdelay_max_picosec
        self.elevation_tropshape = np.expand_dims(np.swapaxes(self.elevation, 0, 1), 1) # reshaped for troposphere operations
        self.opacity, self.sky_temp = self.trop_return_opacity_sky_temp()
        self.transmission = np.exp(-1*self.opacity)

        # Set some optional arrays to None. These will be filled later depending upon the user request.
        self.transmission_matrix = None
        self.turb_phase_errors = None
        self.delay_alltimes = None
        self.sky_noise = None
        
        ### bandpass information
        self.bandpass_table = bandpass_table
        self.bandpass_freq_interp_order = bandpass_freq_interp_order

        ### uv_jones information - G, D, and P-Jones (automatically enabled if D is enabled) matrices
        self.uvjones_g_on = uvjones_g_on
        self.uvjones_d_on = uvjones_d_on
        self.feed_angle = np.deg2rad(feed_angle)
        self.parang_corrected = parang_corrected

        self.gR_mean = gR_mean
        self.gR_std = gR_std
        self.gL_mean = gL_mean
        self.gL_std = gL_std

        self.dR_mean = dR_mean
        self.dR_std = dR_std
        self.dL_mean = dL_mean
        self.dL_std = dL_std

        # Get timestamp at the start of the data generation
        self.timestamp = int(time.time())

        # save zenith transmission
        if (self.trop_enabled):
            np.save(II('$OUTDIR')+'/zenith_transmission_timestamp_%d'%(self.timestamp), self.transmission)

    def interferometric_sim(self):
        """FFT + UV sampling via the MeqTrees run function"""

        ### for static sky - single input FITS image, ASCII file or Tigger LSM ###
        if os.path.exists(self.input_fitsimage+'.txt') == True:
            self.input_fitsimage = self.input_fitsimage+'.txt'
            info('Input sky model is assumed static, given single input ASCII LSM file. Using MeqTrees for predicting visibilities.')
            run_turbosim(self.input_fitsimage,self.output_column,'')
            if self.output_column != 'MODEL_DATA':
                copy_between_cols('MODEL_DATA', self.output_column) # INI: copy uncorrupted vis to MODEL_DATA

        elif os.path.exists(self.input_fitsimage+'.html') == True:
            self.input_fitsimage = self.input_fitsimage+'.html'
            info('Input sky model is assumed static, given single input Tigger LSM file (MeqTrees-specific). Using MeqTrees for predicting visibilities.')
            run_turbosim(self.input_fitsimage,self.output_column,'')
            if self.output_column != 'MODEL_DATA':
                copy_between_cols('MODEL_DATA', self.output_column) # INI: copy uncorrupted vis to MODEL_DATA

        ### INI: if fits image(s), input a directory. Follow conventions for time and polarisation variability.
        elif os.path.isdir(self.input_fitsimage):
            self.input_fitsimage_list = np.sort(glob.glob(os.path.join(self.input_fitsimage,'./*')))
            if self.input_fitspol == 0:
                self.num_images = len(self.input_fitsimage_list)/self.input_changroups
            elif self.input_fitspol == 1 and len(self.input_fitsimage_list)%4 == 0:
                self.num_images = len(self.input_fitsimage_list)/self.input_changroups/4
            else:
                abort("Not all polarisation images are present but 'input_fitspol' is set to True!!!")
            self.vis_per_image = np.floor(self.time_unique.shape[0]/self.num_images)

            startvis = 0
            endvis = self.vis_per_image
            # INI: cycle through images and simulate including polarisation info, if present.
            for img_ind in range(int(self.num_images)):
                temp_input_fits = '%s/t%04d'%(self.input_fitsimage,img_ind)
                info('Simulating visibilities (corr dumps) from %d to %d using input sky model %s'%(startvis,endvis,temp_input_fits))
                run_wsclean(temp_input_fits, self.input_fitspol, self.input_changroups, startvis, endvis, self.oversampling)
                startvis = endvis
                if img_ind != self.num_images-2:
                    endvis = endvis + self.vis_per_image
                else:
                    endvis = endvis + 2*self.vis_per_image # INI: ensure all vis at the end are accounted for in the next (last) iteration.

            # INI: Copy over data from MODEL_DATA to output_column if output_column is not MODEL_DATA
            if self.output_column != 'MODEL_DATA':
                copy_between_cols(self.output_column, 'MODEL_DATA')

        else:
            abort('Problem with input sky models.')

        tab = pt.table(self.msname, readonly=True, ack=False)
        self.data = tab.getcol(self.output_column) 
        tab.close()

    def copy_MS(self, new_name):
        x.sh('cp -r %s %s' % (self.msname, new_name))

    def save_data(self):
        """All saving of data goes through this function"""
        tab = pt.table(self.msname, readonly=False,ack=False)
        tab.putcol(self.output_column, self.data)
        tab.close()
        
    def compute_weights(self):
        """ Compute thermal noise """
        #if rms.shape != self.data.shape:
        #    abort('The rms array used to populate SIGMA, SIGMA_SPECTRUM, WEIGHT, and WEIGHT_SPECTRUM does not have the expected dimensions:\n'\
        #          'rms.shape = '+rms.shape+'. Expected dimensions: '+self.data.shape)

        for a0 in range(self.Nant):
            for a1 in range(self.Nant):
                if a1 > a0:
                    self.receiver_rms[self.baseline_dict[(a0,a1)]] = (1/self.corr_eff) * np.sqrt(self.SEFD[a0] * self.SEFD[a1] / float(2 * self.tint * self.chan_width))


    def add_weights(self, additional_noise_terms=None):
        """ Populate SIGMA, SIGMA_SPECTRUM, WEIGHT, WEIGHT_SPECTRUM columns in the MS """
        #if rms.shape != self.data.shape:
        #    abort('The rms array used to populate SIGMA, SIGMA_SPECTRUM, WEIGHT, and WEIGHT_SPECTRUM does not have the expected dimensions:\n'\
        #          'rms.shape = '+rms.shape+'. Expected dimensions: '+self.data.shape)

        if additional_noise_terms is not None:
            try:
              for tind in range(self.nchunks):
                self.receiver_rms[tind*self.chunksize:(tind+1)*self.chunksize] = np.sqrt(np.power(self.receiver_rms[tind*self.chunksize:(tind+1)*self.chunksize], 2) + np.power(additional_noise_terms[tind*self.chunksize:(tind+1)*self.chunksize], 2))
            except MemoryError:
              abort("Arrays too large to be held in memory. Aborting execution.")

        tab = pt.table(self.msname, readonly=False,ack=False)
        tab.putcol("SIGMA", self.receiver_rms[:,0,:])
        if 'SIGMA_SPECTRUM' in tab.colnames():
            tab.putcol("SIGMA_SPECTRUM", self.receiver_rms)
        tab.putcol("WEIGHT", 1/self.receiver_rms[:,0,:]**2)
        if 'WEIGHT_SPECTRUM' in tab.colnames():
            tab.putcol("WEIGHT_SPECTRUM", 1/self.receiver_rms**2)
        tab.close()


    def add_receiver_noise(self, load=None):
        """ baseline dependent thermal noise only. Calculated from SEFDs, tint, dnu """
        if load:
            self.thermal_noise = np.load(II('$OUTDIR')+'/receiver_noise.npy')
        else:
            info('Instantiating thermal noise...')
            self.thermal_noise = np.zeros(self.data.shape, dtype='complex')
            size = (self.time_unique.shape[0], self.chan_freq.shape[0], 4)
            for a0 in range(self.Nant):
                for a1 in range(self.Nant):
                    if a1 > a0:
                        rms = self.receiver_rms[self.baseline_dict[(a0,a1)]]
                        self.thermal_noise[self.baseline_dict[(a0, a1)]] = self.rng_predict.normal(0.0, rms, size=size) + 1j * self.rng_predict.normal(0.0, rms, size=size)

            np.save(II('$OUTDIR')+'/receiver_noise_timestamp_%d'%(self.timestamp), self.thermal_noise)
        try:
          info('Applying thermal noise to data...')
          for tind in range(self.nchunks):
            self.data[tind*self.chunksize:(tind+1)*self.chunksize] += self.thermal_noise[tind*self.chunksize:(tind+1)*self.chunksize]
          self.save_data()
        except MemoryError:
          abort("Arrays too large to be held in memory. Aborting execution.")

        
    def make_baseline_dictionary(self):
        return dict([((x, y), np.where((self.A0 == x) & (self.A1 == y))[0])
                    for x in self.ant_unique for y in self.ant_unique if y > x])

    def parallactic_angle_calc(self):
        measure = pm.measures()
        ra = qa.quantity(self.direction[0], 'rad'); dec = qa.quantity(self.direction[1], 'rad')
        pointing = measure.direction('j2000', ra, dec)
        start_time = measure.epoch('utc', qa.quantity(self.time_unique[0], 's'))
        measure.doframe(start_time)

        parang_matrix = np.zeros((self.Nant, self.time_unique.shape[0]))

        def antenna_parang(antenna):
            x = qa.quantity(self.pos[antenna, 0], 'm')
            y = qa.quantity(self.pos[antenna, 1], 'm')
            z = qa.quantity(self.pos[antenna, 2], 'm')
            position = measure.position('wgs84', x, y, z)
            measure.doframe(position)
            sec2rad = 2 * np.pi / (24 * 3600.)
            hour_angle = measure.measure(pointing, 'HADEC')['m0']['value'] +\
                         (self.time_unique-self.time_unique.min()) * sec2rad
            earth_radius = 6371000.0
            latitude = np.arcsin(self.pos[antenna, 2]/earth_radius)
            return np.arctan2(np.sin(hour_angle)*np.cos(latitude), (np.cos(self.direction[1])*np.sin(latitude)-np.cos(hour_angle)*np.cos(latitude)*\
                   np.sin(self.direction[1])))

        for i in range(self.Nant):
            parang_matrix[i] = antenna_parang(i)

        return parang_matrix
    
    def elevation_calc(self):
        measure = pm.measures()
        ra = qa.quantity(self.direction[0], 'rad'); dec = qa.quantity(self.direction[1], 'rad')
        pointing = measure.direction('j2000', ra, dec)
        start_time = measure.epoch('utc', qa.quantity(self.time_unique[0], 's'))
        measure.doframe(start_time)

        elevation_ant_matrix = np.zeros((self.Nant, self.time_unique.shape[0]))

        def antenna_elevation(antenna):
            x = qa.quantity(self.pos[antenna, 0], 'm')
            y = qa.quantity(self.pos[antenna, 1], 'm')
            z = qa.quantity(self.pos[antenna, 2], 'm')
            position = measure.position('wgs84', x, y, z)
            measure.doframe(position)
            sec2rad = 2 * np.pi / (24 * 3600.)
            hour_angle = measure.measure(pointing, 'HADEC')['m0']['value'] +\
                         (self.time_unique-self.time_unique.min()) * sec2rad
            earth_radius = 6371000.0
            latitude = np.arcsin(self.pos[antenna, 2]/earth_radius)
            return np.arcsin(np.sin(latitude)*np.sin(self.direction[1])+np.cos(latitude)*np.cos(self.direction[1]) *
                             np.cos(hour_angle))

        for i in range(self.Nant):
            elevation_ant_matrix[i] = antenna_elevation(i)
    
        return elevation_ant_matrix


    def calc_ant_rise_set_times(self):
        self.mjd_ant_rise = np.zeros(self.Nant)
        self.mjd_ant_set = np.zeros(self.Nant)
        for ant in range(self.Nant):
            try:
                self.mjd_ant_rise[ant] = self.time_unique[np.logical_not(np.isnan(self.elevation[ant,:]))].min()
                self.mjd_ant_set[ant] = self.time_unique[np.logical_not(np.isnan(self.elevation[ant,:]))].max()
            except ValueError:
                self.mjd_ant_rise[ant] = np.inf
                self.mjd_ant_set[ant] = -np.inf

    def calculate_baseline_min_elevation(self):
        self.baseline_min_elevation = np.zeros(len(self.uvw[:,0]))
        temp_elevation = self.elevation.copy()
        temp_elevation[np.isnan(temp_elevation)] = 1000. # set nan's high. Flags used in plotting
        #elevation_mask = temp_elevation < 90.
        for ant0 in range(self.Nant):
            for ant1 in range(self.Nant):
                if (ant1 > ant0):
                    self.baseline_min_elevation[self.baseline_dict[(ant0,ant1)]] = \
                        np.min(np.vstack([temp_elevation[ant0, :], temp_elevation[ant1, :]]), axis=0)


    def calculate_baseline_mean_elevation(self):
        self.baseline_mean_elevation = np.zeros(len(self.uvw[:,0]))
        temp_elevation = self.elevation.copy()
        temp_elevation[np.isnan(temp_elevation)] = 1000. # set nan's high. Flags used in plotting
        #elevation_mask = temp_elevation < 90.
        for ant0 in range(self.Nant):
            for ant1 in range(self.Nant):
                if (ant1 > ant0):
                    self.baseline_mean_elevation[self.baseline_dict[(ant0,ant1)]] = \
                        np.mean(np.vstack([temp_elevation[ant0, :], temp_elevation[ant1, :]]), axis=0)

    
    def write_flag(self, elevation_limit):
        """ flag data if below user-specified elevation limit """
        for a0 in range(self.Nant):
            for a1 in range(self.Nant):
                if a1 > a0:
                    flag_mask = np.invert(((self.elevation[a1] > elevation_limit) &
                                           (self.elevation[a0] > elevation_limit)) > 0)
                    self.flag[self.baseline_dict[(a0, a1)]] = flag_mask.reshape((flag_mask.shape[0], 1, 1))

        tab = pt.table(self.msname, readonly=False,ack=False)
        tab.putcol("FLAG", self.flag)
        info('FLAG column re-written using antenna elevation limit(s)')
        tab.close()


    def trop_opacity_attenuate(self):
        transmission_matrix = np.exp(-1 * self.opacity / np.sin(self.elevation_tropshape))
        np.save(II('$OUTDIR')+'/transmission_timestamp_%d'%(self.timestamp), transmission_matrix)

        transmission_matrix = np.expand_dims(transmission_matrix, 3)
        self.transmission_matrix = transmission_matrix
        transmission_column = np.zeros(self.data.shape)
        for a0 in range(self.Nant):
            for a1 in range(self.Nant):
                if a1 > a0:
                    transmission_column[self.baseline_dict[(a0, a1)]] = np.sqrt(transmission_matrix[:, :, a0, :] * transmission_matrix[:, :, a1, :])

        self.data = np.multiply(self.data, transmission_column)
        self.save_data()


    def trop_return_opacity_sky_temp(self):
        """ as it says on the tin """
        opacity, sky_temp = np.zeros((2, 1, self.chan_freq.shape[0], self.Nant))

        for ant in range(self.Nant):
            if not os.path.exists(II('$OUTDIR')+'/atm_output/'):
                os.makedirs(II('$OUTDIR')+'/atm_output/')

            fmin,fmax,fstep = (self.chan_freq[0]-(self.chan_width)) / 1e9,\
                              (self.chan_freq[-1]) / 1e9, \
                              self.chan_width/1e9    # note that fmin output != self.chan_freq
            ATM_abs_string = 'absorption --fmin %f --fmax %f --fstep %f --pwv %f --gpress %f --gtemp %f' %\
                             (fmin, fmax, fstep, self.average_pwv[ant], \
                              self.average_gpress[ant],self.average_gtemp[ant])
            
            #output = subprocess.check_output(self.ATM_absorption_string(self.chan_freq[0]-self.chan_width, self.chan_freq[-1], self.chan_width,self.average_pwv[ant], self.average_gpress[ant],self.average_gtemp[ant]),shell=True)
            output = subprocess.check_output(ATM_abs_string,shell=True)
            atmfile = open(II('$OUTDIR')+'/atm_output/ATMstring_ant%i.txt'%ant,'w')
            print(ATM_abs_string, file=atmfile)
            atmfile.close()

            with open(II('$OUTDIR')+'/atm_output/%satm_abs.txt'%ant, 'wb') as atm_abs:
                atm_abs.write(output)

            if self.num_chan == 1:
                freq_atm, dry, wet, temp_atm = np.swapaxes(np.expand_dims(np.loadtxt(II('$OUTDIR')+'/atm_output/%satm_abs.txt'%ant, skiprows=1, usecols=[0, 1, 2, 3], delimiter=', \t'), axis=0), 0, 1)
            else:
                freq_atm,dry, wet, temp_atm = np.swapaxes(np.loadtxt(II('$OUTDIR')+'/atm_output/%satm_abs.txt'%ant, skiprows=1, usecols=[0, 1, 2, 3], delimiter=', \t'), 0, 1)
            # the following catch is due to a bug in the ATM package
            # which results in an incorrect number of channels returned.
            # The following section just checks and corrects for that. 

            if (len(self.chan_freq) != len(freq_atm)):
                abort('!!! ERROR: THIS IS AN AATM bug. !!!\n\t'+\
                 'number of frequency channels in ATM output = %i\n\t'\
                    %len(freq_atm)+\
                'which is NOT equal to Measurement Set nchans = %i\n\t'\
                    %len(self.chan_freq)+ 'which was requested.'+\
                '\nThe AATM developers have been contacted about\n'+\
                'this bug, however, an interim workaround is to select\n'+\
                'a slightly different channel width and/or number of channels')

            if (self.trop_wetonly == 1):
                opacity[:, :, ant] = wet
            else:
                opacity[:,:,ant] = dry + wet

            sky_temp[:, :, ant] = temp_atm
        return opacity, sky_temp


    def trop_add_sky_noise(self, load=None):
        """ for non-zero tropospheric opacity, calc sky noise"""

        if load:
            """this option to load same noise not available in new MeqSilhouette version"""
            self.sky_noise = np.load(II('$OUTDIR')+'/atm_output/sky_noise.npy')

        else:

            sefd_matrix = 2 * Boltzmann / self.dish_area * (1e26*self.sky_temp * (1. - np.exp(-1.0 * self.opacity / np.sin(self.elevation_tropshape))))
            self.sky_noise = np.zeros(self.data.shape, dtype='complex')
            sky_sigma_estimator = np.zeros(self.data.shape)

            for a0 in range(self.Nant):
                for a1 in range(self.Nant):
                    if a1 > a0:
                        rms = (1/self.corr_eff) * np.sqrt(sefd_matrix[:, :, a0] * sefd_matrix[:, :, a1] / (float(2 * self.tint * self.chan_width)))
                        self.temp_rms = rms
                        rms = np.expand_dims(rms, 2)
                        rms = rms * np.ones((1, 1, 4))
                        self.sky_noise[self.baseline_dict[(a0, a1)]] = self.rng_atm.normal(0.0, rms) + 1j * self.rng_atm.normal(0.0, rms)
                        sky_sigma_estimator[self.baseline_dict[(a0, a1)]] = rms
            np.save(II('$OUTDIR')+'/atm_output/sky_noise_timestamp_%d'%(self.timestamp), self.sky_noise)
        try:
          for tind in range(self.nchunks):
            self.data[tind*self.chunksize:(tind+1)*self.chunksize] += self.sky_noise[tind*self.chunksize:(tind+1)*self.chunksize]          
          self.save_data()
        except:
          abort("Arrays too large to be held in memory. Aborting execution.")

        return sky_sigma_estimator
        

    def trop_generate_turbulence_phase_errors(self):
        turb_phase_errors = np.zeros((self.time_unique.shape[0], self.chan_freq.shape[0], self.Nant))
        beta = 5/3. # power law index

        time_indices = np.arange(self.time_unique.shape[0]) # INI: to index the autocorrelation function
        time_in_secs = self.time_unique - self.time_unique[0] # to compute the structure function
        (x,y) = np.meshgrid(time_indices, time_indices)

        for ant in np.arange(self.Nant):
            structD = np.power((time_in_secs/self.coherence_time[ant]), beta) # compute structure function
            autocorrC = np.abs(0.5*(structD[-1]-structD)) # compute autocorrelation function, clipped at largest mode
            covmatS = autocorrC[np.abs(x-y)] # compute covariance matrix
            L = np.linalg.cholesky(covmatS) # Cholesky factorise the covariance matrix
            
            # INI: generate random walk error term
            turb_phase_errors[:, 0, ant] = np.sqrt(1/np.sin(self.elevation_tropshape[:, 0, ant])) * L.dot(self.rng_atm.standard_normal(self.time_unique.shape[0]))
            turb_phase_errors[:, :, ant] = np.multiply(turb_phase_errors[:, 0, ant].reshape((self.time_unique.shape[0], 1)), (self.chan_freq/self.chan_freq[0]).reshape((1, self.chan_freq.shape[0])))

        self.turb_phase_errors = turb_phase_errors
        np.save(II('$OUTDIR')+'/turbulent_phase_errors_timestamp_%d'%(self.timestamp), turb_phase_errors)

    def trop_calc_fixdelay_phase_offsets(self):
        """insert constant delay for each station for all time stamps. 
        Used for testing fringe fitters"""
        delay = self.trop_ATM_dispersion() / speed_of_light
        self.delay_alltimes = delay / np.sin(self.elevation_tropshape)
        phasedelay_alltimes = 2*np.pi * delay / np.sin(self.elevation_tropshape) * self.chan_freq.reshape((1, self.chan_freq.shape[0], 1))
        mean_phasedelays = np.nanmean(phasedelay_alltimes,axis=0)
        phasedelay_alltimes_iter = range(len(phasedelay_alltimes))
        for i in phasedelay_alltimes_iter:
            phasedelay_alltimes[i] = mean_phasedelays
        np.save(II('$OUTDIR')+'/atm_output/phasedelay_alltimes_timestamp_%d'%(self.timestamp), phasedelay_alltimes)
        np.save(II('$OUTDIR')+'/atm_output/delay_alltimes_timestamp_%d'%(self.timestamp), self.delay_alltimes)

        self.fixdelay_phase_errors = phasedelay_alltimes

    
    def trop_ATM_dispersion(self):
        """ calculate extra path length per frequency channel """
        extra_path_length = np.zeros((self.chan_freq.shape[0], self.Nant))

        for ant in range(self.Nant):

            fmin,fmax,fstep = (self.chan_freq[0]-(self.chan_width)) / 1e9,\
                              (self.chan_freq[-1]) / 1e9, \
                              self.chan_width/1e9 # note that fmin output != self.chan_freq
            ATM_disp_string = 'dispersive --fmin %f --fmax %f --fstep %f --pwv %f --gpress %f --gtemp %f' %\
                             (fmin, fmax, fstep, self.average_pwv[ant], \
                              self.average_gpress[ant],self.average_gtemp[ant])
            
            output = subprocess.check_output(ATM_disp_string,shell=True)
            atmfile = open(II('$OUTDIR')+'/atm_output/ATMstring_ant%i.txt'%ant,'a')
            print(ATM_disp_string, file=atmfile)
            atmfile.close()

            with open(II('$OUTDIR')+'/atm_output/%satm_disp.txt'%ant, 'wb') as atm_disp:
                atm_disp.write(output)

            if self.num_chan == 1:
                wet_non_disp, wet_disp, dry_non_disp = np.swapaxes(np.expand_dims(np.genfromtxt(II('$OUTDIR')+'/atm_output/%satm_disp.txt'%ant, skip_header=1, usecols=[1, 2, 3], delimiter=',',autostrip=True), axis=0), 0, 1)
            else:
                wet_non_disp, wet_disp, dry_non_disp = np.swapaxes(np.genfromtxt(II('$OUTDIR')+'/atm_output/%satm_disp.txt'%ant, skip_header=1, usecols=[1, 2, 3], delimiter=',',autostrip=True), 0, 1)
            if (self.trop_wetonly):
                extra_path_length[:, ant] = wet_disp + wet_non_disp
            else:
                extra_path_length[:, ant] = wet_disp + wet_non_disp  + dry_non_disp


            np.save(II('$OUTDIR')+'/atm_output/delay_norm_ant%i_timestamp_%d'%(ant, self.timestamp), extra_path_length[:,ant] / speed_of_light)

        np.save(II('$OUTDIR')+'/atm_output/delay_norm_timestamp_%d'%(self.timestamp), extra_path_length / speed_of_light)

            
        return extra_path_length
    

    def trop_calc_mean_delays(self):
        """ insert mean delays (i.e. non-turbulent) due to dry and wet components"""
        delay = self.trop_ATM_dispersion() / speed_of_light
        self.delay_alltimes = delay / np.sin(self.elevation_tropshape)
        phasedelay_alltimes = 2*np.pi * delay / np.sin(self.elevation_tropshape) * self.chan_freq.reshape((1, self.chan_freq.shape[0], 1))
        np.save(II('$OUTDIR')+'/atm_output/phasedelay_alltimes_timestamp_%d'%(self.timestamp), phasedelay_alltimes)
        np.save(II('$OUTDIR')+'/atm_output/delay_alltimes_timestamp_%d'%(self.timestamp), self.delay_alltimes)

        self.phasedelay_alltimes = phasedelay_alltimes 


    def trop_phase_corrupt(self, normalise=True, percentage_turbulence=100, load=None):
        ### REPLACE WITH A GENERATE TROP SIM COORDINATOR, THAT COLLECTS ALL TROP COORUPTIONS """
        """only supports different channels but not different subbands"""

        if load:
            info('Loading previously saved phase errors from file:\n'+\
                 II('$OUTDIR')+'/turbulent_phases.npy')
            errors = np.load(II('$OUTDIR')+'/turbulent_phases.npy')

        else:
            errors = self.calc_phase_errors()

        errors *= percentage_turbulence/100.

        if normalise:
            errors += self.phase_normalisation()

        for a0 in range(self.Nant):
            for a1 in range(self.Nant):
                if a1 > a0:
                    # the following line was errors[:,:,a1] - errors[:,:,a0], 
                    # swopped it around to get right delay signs from AIPS
                    error_column = np.exp(1j * (errors[:, :, a0] - errors[:, :, a1]))
                    self.data[self.baseline_dict[(a0, a1)]] = np.multiply(self.data[self.baseline_dict[(a0, a1)]],
                                                                          np.expand_dims(error_column, 2))
        self.save_data()
        info('Kolmogorov turbulence-induced phase fluctuations applied')


    def apply_phase_errors(self,combined_phase_errors):
        for a0 in range(self.Nant):
            for a1 in range(self.Nant):
                if a1 > a0:
                    # the following line was errors[:,:,a1] - errors[:,:,a0],
                    # swopped it around to get right delay signs from AIPS
                    error_column = np.exp(1j * (combined_phase_errors[:, :, a0] \
                                            - combined_phase_errors[:, :,a1]))
                    self.data[self.baseline_dict[(a0, a1)]] = \
                        np.multiply(self.data[self.baseline_dict[(a0, a1)]],\
                        np.expand_dims(error_column, 2))
        self.save_data()

        
    def trop_plots(self):

        ### plot zenith opacity vs frequency (subplots)
        '''fig,axes = pl.subplots(self.Nant,1,figsize=(10,16))
        #color.cycle_cmap(self.Nant,cmap=cmap) # INI: deprecated
        #colors = [pl.cm.Set1(i) for i in np.linspace(0, 1, self.nbl)]
        #axes.set_prop_cycle(cycler('color', colors))

        for i,ax in enumerate(axes.flatten()):
            ax.plot(self.chan_freq/1e9,self.transmission[0,:,i],label=self.station_names[i])
            ax.legend(prop={'size':12})
            ax.set_ylim(np.nanmin(self.transmission),1)
        pl.xlabel('Frequency / GHz', fontsize=FSIZE)
        pl.ylabel('Transmission', fontsize=FSIZE)
        pl.tight_layout()
        pl.savefig(os.path.join(v.PLOTDIR,'zenith_transmission_vs_freq_subplots.png'),bbox_inches='tight')
        pl.close()'''

        ### plot zenith opacity vs frequency
        if self.num_chan > 1:
            pl.figure(figsize=(10,6.8))
            #color.cycle_cmap(self.Nant, cmap=cmap) # INI: deprecated
            for i in range(self.Nant):
                pl.plot(self.chan_freq/1e9,self.transmission[0,:,i],label=self.station_names[i])
            pl.xlabel('Frequency / GHz', fontsize=FSIZE)
            pl.ylabel('Zenith transmission', fontsize=FSIZE)
            pl.xticks(fontsize=18)
            pl.yticks(fontsize=18)
            lgd = pl.legend(bbox_to_anchor=(1.02,1),loc=2,shadow=True)
            pl.savefig(os.path.join(v.PLOTDIR,'zenith_transmission_vs_freq.png'),\
                       bbox_extra_artists=(lgd,), bbox_inches='tight')
            pl.close()

        ### plot elevation-dependent transmission vs frequency
        if self.transmission_matrix is not None:
          pl.figure() #figsize=(10,6.8))
          for i in range(self.Nant):
            pl.imshow(self.transmission_matrix[:,:,i,0],origin='lower',aspect='auto',\
                      extent=[(self.chan_freq[0]-(self.chan_width/2.))/1e9,(self.chan_freq[-1]+(self.chan_width/2.))/1e9,0,self.obslength/3600.])
            pl.xlabel('Frequency / GHz', fontsize=16)
            pl.ylabel('Relative time / hr', fontsize=16)
            pl.xticks(fontsize=14)
            pl.yticks(fontsize=14)            
            cb = pl.colorbar()
            cb.set_label('Transmission', fontsize=12)
            cb.ax.tick_params(labelsize=12)
            pl.title(self.station_names[i])
            pl.tight_layout()
            pl.savefig(os.path.join(v.PLOTDIR,'transmission_vs_freq_%s.png'%self.station_names[i]))
            pl.clf()

        ### plot zenith sky temp vs frequency
        if self.num_chan > 1:
            pl.figure(figsize=(10,6.8))
            #color.cycle_cmap(self.Nant, cmap=cmap) # INI: deprecated
            for i in range(self.Nant):
                pl.plot(self.chan_freq/1e9,self.sky_temp[0,:,i],label=self.station_names[i])
            pl.xlabel('Frequency / GHz', fontsize=FSIZE)
            pl.ylabel('Zenith sky temperature / K', fontsize=FSIZE)
            pl.xticks(fontsize=18)
            pl.yticks(fontsize=18)        
            lgd = pl.legend(bbox_to_anchor=(1.02,1),loc=2,shadow=True)
            pl.savefig(os.path.join(v.PLOTDIR,'zenith_skytemp_vs_freq.png'),\
                       bbox_extra_artists=(lgd,), bbox_inches='tight')
            pl.close()
                                                                                            
        ### plot tubulent phase on time/freq grid?
        if self.turb_phase_errors is not None:
          pl.figure() #figsize=(10,6.8))
          for i in range(self.Nant):
            pl.imshow( (self.turb_phase_errors[:,:,i] * 180. / np.pi) ,origin='lower',aspect='auto',\
                      extent=[(self.chan_freq[0]-(self.chan_width/2.))/1e9,(self.chan_freq[-1]+(self.chan_width/2.))/1e9,0,self.obslength/3600.]) #vmin=-180,180
            pl.xlabel('Frequency / GHz', fontsize=FSIZE)
            pl.ylabel('Relative time / hr', fontsize=FSIZE)
            pl.xticks(fontsize=18)
            pl.yticks(fontsize=18)            
            cb = pl.colorbar()
            cb.set_label('Turbulent phase / degrees')
            pl.title(self.station_names[i])
            pl.tight_layout()
            pl.savefig(os.path.join(v.PLOTDIR,'turbulent_phase_waterfall_plot_%s.png'%self.station_names[i]))
            pl.clf()
                                                                                                                            
        ### plot turbulent phase errors vs time
        if self.turb_phase_errors is not None:
          pl.figure(figsize=(10,6.8))
          #color.cycle_cmap(self.Nant, cmap=cmap) # INI: deprecated
          for i in range(self.Nant):
            pl.plot(np.linspace(0,self.obslength,len(self.time_unique))/(60*60.),\
                    (self.turb_phase_errors[:,0,i]*180./np.pi),\
                    label=self.station_names[i],alpha=1)
          pl.xlabel('Relative time / hr', fontsize=FSIZE)
          pl.ylabel('Turbulent phase / degrees', fontsize=FSIZE)
          pl.xticks(fontsize=18)
          pl.yticks(fontsize=18)        
          #pl.ylim(-180,180)
          #lgd = pl.legend(bbox_to_anchor=(1.02,1),loc=2,shadow=True)
          lgd = pl.legend()
          pl.savefig(os.path.join(v.PLOTDIR,'turbulent_phase_vs_time.png'),\
                     bbox_extra_artists=(lgd,), bbox_inches='tight')
          pl.close()

        ### plot delays vs time
        if self.delay_alltimes is not None:
          pl.figure(figsize=(10,6.8))
          #color.cycle_cmap(self.Nant, cmap=cmap) # INI: deprecated
          try:
            delay_temp = fixdelay_phase_errors # checks if fixdelays are set
          except NameError:
            delay_temp = self.delay_alltimes
          for i in range(self.Nant):
            pl.plot(np.linspace(0,self.obslength,len(self.time_unique))/(60*60.),\
                    np.nanmean(delay_temp[:,:,i],axis=1) * 1e9,label=self.station_names[i])
          pl.xlabel('Relative time / hr', fontsize=FSIZE)
          pl.ylabel('Delay / ns', fontsize=FSIZE)
          pl.xticks(fontsize=18)
          pl.yticks(fontsize=18)        
          lgd = pl.legend(bbox_to_anchor=(1.02,1),loc=2,shadow=True)
          pl.savefig(os.path.join(v.PLOTDIR,'mean_delay_vs_time.png'),\
                   bbox_extra_artists=(lgd,), bbox_inches='tight')
          pl.close()


        ################################
        ### POINTING ERROR FUNCTIONS ###
        ################################

    def pointing_constant_offset(self,pointing_rms, pointing_timescale,PB_FWHM230):
            """this will change the pointing error for each antenna every pointing_timescale
            which one of could essentially think of as a scan length (e.g. 10 minutes)"""
            self.PB_FWHM = PB_FWHM230 / (self.chan_freq.mean() / 230e9) # convert 230 GHz PB to current obs frequency
            self.num_mispoint_epochs = max(1, int(round(self.obslength / (pointing_timescale * 60.), 0))) # could be number of scans, for example
            self.mjd_per_ptg_epoch = (self.mjd_obs_end - self.mjd_obs_start) / self.num_mispoint_epochs
            self.mjd_ptg_epoch_timecentroid = np.arange(self.mjd_obs_start,self.mjd_obs_end,
                                                        self.mjd_per_ptg_epoch) + (self.mjd_per_ptg_epoch/2.)
            # handle potential rounding error
            if self.num_mispoint_epochs != len(self.mjd_ptg_epoch_timecentroid):
                self.mjd_ptg_epoch_timecentroid = self.mjd_ptg_epoch_timecentroid[:-1]

            self.pointing_offsets = pointing_rms.reshape(self.Nant,1) * self.rng_predict.standard_normal((self.Nant,self.num_mispoint_epochs)) # units: arcsec
            for ant in range(self.Nant):
                ind = (self.mjd_ptg_epoch_timecentroid < self.mjd_ant_rise[ant]) \
                    | (self.mjd_ptg_epoch_timecentroid > self.mjd_ant_set[ant])

                self.pointing_offsets[ant,ind] = np.nan # this masks out pointing offsets for stowed antennas

            PB_model = ['gaussian']*self.Nant # primary beam model set in input config file. Hardwired to Gaussian for now. 

            amp_errors = np.zeros([self.Nant,self.num_mispoint_epochs])
            for ant in range(self.Nant):
                if PB_model[ant] == 'cosine3':
                    amp_errors[ant,:] = np.cos(self.pointing_offsets[ant,:]/206265.)**3 #placeholder, incorrect

                elif PB_model[ant] == 'gaussian':
                    amp_errors[ant,:] = np.exp(-0.5*(self.pointing_offsets[ant,:]/(self.PB_FWHM[ant]/2.35))**2) 

                    
            self.pointing_amp_errors = amp_errors


    def apply_pointing_amp_error(self):
            for a0 in range(self.Nant):
                for a1 in range(self.Nant):
                    if a1 > a0:
                        bl_indices = self.baseline_dict[(a0,a1)] # baseline indices only, all times
                        for mispoint_epoch in range(self.num_mispoint_epochs):
                            epoch_ind_mask = (self.time[bl_indices] >= ( self.mjd_obs_start + (mispoint_epoch*self.mjd_per_ptg_epoch))) &\
                                       (self.time[bl_indices] <= ( self.mjd_obs_start + (mispoint_epoch+1)*self.mjd_per_ptg_epoch))
                                        
                                         # need to add a elevation mask 
                            bl_epoch_indices = bl_indices[epoch_ind_mask]
                            
                                        
            
                            self.data[bl_epoch_indices,:,:] *= (self.pointing_amp_errors[a0,mispoint_epoch] \
                                                                * self.pointing_amp_errors[a1,mispoint_epoch])
                        #### NOTE: this applies to all pols, all frequency channels (i.e. no primary beam freq dependence)
                        #self.data[indices,0,3] *= (self.point_amp_errors[a0] * self.point_amp_errors[a1])
            self.save_data()


    def plot_pointing_errors(self):

        ### plot antenna offset vs pointing epoch
        pl.figure(figsize=(10,6.8))
        #color.cycle_cmap(self.Nant, cmap=cmap) # INI: deprecated
        for i in range(self.Nant):
            pl.plot(np.linspace(0,self.obslength/3600,self.num_mispoint_epochs),self.pointing_offsets[i,:],alpha=1,label=self.station_names[i])
        pl.xlabel('Relative time / hr', fontsize=FSIZE)
        pl.ylabel('Pointing offset / arcsec', fontsize=FSIZE) 
        lgd = pl.legend(bbox_to_anchor=(1.02,1),loc=2,shadow=True)
        pl.savefig(os.path.join(v.PLOTDIR,'pointing_angular_offset_vs_time.png'),\
                   bbox_extra_artists=(lgd,), bbox_inches='tight')
        pl.close()
                                                                                                
        ### plot pointing amp error vs pointing epoch
        pl.figure(figsize=(10,6.8))
        #color.cycle_cmap(self.Nant, cmap=cmap) # INI: deprecated
        for i in range(self.Nant):
            pl.plot(np.linspace(0,self.obslength/3600,self.num_mispoint_epochs),self.pointing_amp_errors[i,:],alpha=1,label=self.station_names[i])
        pl.ylim(np.nanmin(self.pointing_amp_errors[:, :]) * 0.9, 1.04)
        pl.xlabel('Relative time / hr', fontsize=FSIZE)
        pl.ylabel('Primary beam response', fontsize=FSIZE)        
        lgd = pl.legend(bbox_to_anchor=(1.02,1),loc=2,shadow=True)
        pl.savefig(os.path.join(v.PLOTDIR,'pointing_amp_error_vs_time.png'),\
                   bbox_extra_artists=(lgd,), bbox_inches='tight')
        pl.close()

        ### plot pointing amp error vs pointing offset
        pl.figure(figsize=(10,6.8))
        #color.cycle_cmap(self.Nant, cmap=cmap) # INI: deprecated
        marker = itertools.cycle(('.', 'o', 'v', '^', 's', '+', '*', 'h', 'D'))
        for i in range(self.Nant):
            pl.plot(abs(self.pointing_offsets[i,:]),self.pointing_amp_errors[i,:], marker=next(marker), linestyle='', alpha=1,label=self.station_names[i])
        pl.xlim(0,np.nanmax(abs(self.pointing_offsets))*1.1)
        pl.ylim(np.nanmin(self.pointing_amp_errors[i,:])*0.8,1.04)
        pl.xlabel(r'Pointing offset, $\rho$ / arcsec', fontsize=FSIZE)
        pl.ylabel('Primary beam response', fontsize=FSIZE) #antenna pointing amplitude error')
        pl.xticks(fontsize=20)
        pl.yticks(fontsize=20)        
        #lgd = pl.legend(bbox_to_anchor=(1.02,1),loc=2,shadow=True)
        lgd = pl.legend(loc='lower left', prop={'size':12})
        pl.savefig(os.path.join(v.PLOTDIR,'pointing_amp_error_vs_angular_offset.png'),\
                   bbox_extra_artists=(lgd,), bbox_inches='tight')
        pl.close()
 

    ################################
    # BANDPASS (B-JONES) FUNCTIONS #
    ################################

    def add_bjones_manual(self):
        """Read in the bandpass info from an ASCII table, interpolate (spline) MS frequencies,
           and apply to data"""
        info("Applying scalar B-Jones amplitudes")
        # Read in the file
        bjones_inp = np.loadtxt(self.bandpass_table,dtype=str)
        self.bpass_input_freq = bjones_inp[0][1:].astype(np.float64)*1e9 # convert from GHz to Hz

        bjones_ampl = bjones_inp[1:,1:]
        self.bjones_ampl_r = np.zeros((bjones_ampl.shape[0],bjones_ampl.shape[1]))
        self.bjones_ampl_l = np.zeros((bjones_ampl.shape[0],bjones_ampl.shape[1]))
        for i in range(bjones_ampl.shape[0]):
            for j in range(bjones_ampl.shape[1]):
                self.bjones_ampl_r[i,j] = ast.literal_eval(bjones_ampl[i,j])[0]
                self.bjones_ampl_l[i,j] = ast.literal_eval(bjones_ampl[i,j])[1]

        # Interpolate between the frequencies given in the bandpass table
        if self.bpass_input_freq[0] > self.chan_freq[0] or self.bpass_input_freq[-1] < self.chan_freq[-1]:
            warn("Input frequencies out of range of MS frequencies. Extrapolating in some places.")

        self.bjones_interpolated=np.zeros((self.Nant,self.chan_freq.shape[0],2,2), dtype=complex)
        for ant in range(self.Nant):
            spl_r = ius(self.bpass_input_freq, self.bjones_ampl_r[ant], k=self.bandpass_freq_interp_order)
            spl_l = ius(self.bpass_input_freq, self.bjones_ampl_l[ant], k=self.bandpass_freq_interp_order)
            #bjones_interpolated[ant] = spl(self.chan_freq)
            temp_amplitudes_r = spl_r(self.chan_freq)
            temp_amplitudes_l = spl_l(self.chan_freq)
            temp_phases_r = np.deg2rad(60*self.rng_predict.random(temp_amplitudes_r.shape[0]) - 30) # add random phases between -30 deg to +30 deg
            temp_phases_l = np.deg2rad(60*self.rng_predict.random(temp_amplitudes_l.shape[0]) - 30) # add random phases between -30 deg to +30 deg
            self.bjones_interpolated[ant,:,0,0] = np.array(list(map(cmath.rect, temp_amplitudes_r, temp_phases_r)))
            self.bjones_interpolated[ant,:,1,1] = np.array(list(map(cmath.rect, temp_amplitudes_l, temp_phases_l)))

        # INI: Write the bandpass gains
        np.save(II('$OUTDIR')+'/bterms_timestamp_%d'%(self.timestamp), self.bjones_interpolated)

        # apply the B-Jones terms by iterating over baselines
        data_reshaped = self.data.reshape((self.data.shape[0],self.data.shape[1],2,2))
        for a0 in range(self.Nant):
            for a1 in range(a0+1,self.Nant):
                bl_ind = self.baseline_dict[(a0,a1)]
                for freq_ind in range(self.chan_freq.shape[0]):
                    data_reshaped[bl_ind,freq_ind] = np.matmul(np.matmul(self.bjones_interpolated[a0,freq_ind], data_reshaped[bl_ind,freq_ind]), np.conjugate(self.bjones_interpolated[a1,freq_ind].T))
        self.data = data_reshaped.reshape(self.data.shape)
        self.save_data()


    def make_bandpass_plots(self):
        ''' Make plots of bandpass amplitudes and phases vs frequency'''
        fig, ax1 = pl.subplots()
        #color.cycle_cmap(self.Nant, cmap=cmap) # INI: deprecated
        for i in range(self.Nant):
            ax1.plot(self.chan_freq/1e9,np.abs(self.bjones_interpolated[i,:,0,0]),label=self.station_names[i])
            #ax1.plot(self.chan_freq,np.abs(self.bjones_interpolated[i,:,0,0]),label=self.station_names[i])
        ax1.set_xlabel('Frequency / GHz', fontsize=18) # was FSIZE
        ax1.set_ylabel('Gain amplitude', fontsize=18)
        ax1.tick_params(axis="x", labelsize=18) # was 18
        ax1.tick_params(axis="y", labelsize=18)
        ax2 = ax1.twiny()
        ax2.set_xlim(0,self.num_chan-1)
        ax2.set_xlabel('Channel', fontsize=18) # was FSIZE
        ax2.tick_params(axis="x", labelsize=18)

        lgd = ax1.legend(prop={'size':11})
        pl.savefig(os.path.join(v.PLOTDIR,'input_bandpasses_ampl_Rpol.png'),\
                   bbox_extra_artists=(lgd,), bbox_inches='tight')

        fig, ax1 = pl.subplots()
        for i in range(self.Nant):
            ax1.plot(self.chan_freq/1e9,np.abs(self.bjones_interpolated[i,:,1,1]),label=self.station_names[i])
            #ax1.plot(self.chan_freq,np.abs(self.bjones_interpolated[i,:,1,1]),label=self.station_names[i])
        ax1.set_xlabel('Frequency / GHz', fontsize=18) # was FSIZE
        ax1.set_ylabel('Gain amplitude', fontsize=18) # was FSIZE
        ax1.tick_params(axis="x", labelsize=18) # was 18
        ax1.tick_params(axis="y", labelsize=18) # was 18
        ax2 = ax1.twiny()
        ax2.set_xlim(0,self.num_chan-1)
        ax2.set_xlabel('Channel', fontsize=18) # was FSIZE
        ax2.tick_params(axis="x", labelsize=18) # was 18

        lgd = ax1.legend(prop={'size':11}) # was 12
        pl.savefig(os.path.join(v.PLOTDIR,'input_bandpasses_ampl_Lpol.png'),\
                   bbox_extra_artists=(lgd,), bbox_inches='tight')
        

    ##################################
    # POLARIZATION LEAKAGE FUNCTIONS #
    ##################################
    def add_pol_leakage_manual(self):
      """ Add constant station-based polarization leakage (D-Jones term) """

      if self.parang_corrected == False:
        # INI: Do not remove parallactic angle rotation effect (vis in antenna plane). Hence, perform 2*field_angle rotation (Leppanen, 1995)
        info("Applying D-terms without correcting for parang rotation. Visibilities are in the antenna plane.")
        # Compute P-Jones matrices
        self.pjones_mat = np.zeros((self.Nant,self.time_unique.shape[0],2,2),dtype=complex)
        self.djones_mat = np.ones((self.Nant,self.num_chan,2,2),dtype=complex)

        for ant in range(self.Nant):
          self.djones_mat[ant,:,0,1] = self.rng_predict.normal(self.dR_mean.real[ant],self.dR_std.real[ant],size=(self.num_chan)) + 1j*self.rng_predict.normal(self.dR_mean.imag[ant],self.dR_std.imag[ant],size=(self.num_chan))
          self.djones_mat[ant,:,1,0] = self.rng_predict.normal(self.dL_mean.real[ant],self.dL_std.real[ant],size=(self.num_chan)) + 1j*self.rng_predict.normal(self.dL_mean.imag[ant],self.dL_std.imag[ant],size=(self.num_chan))

          if self.mount[ant] == 'ALT-AZ':
            self.pjones_mat[ant,:,0,0] = np.exp(-1j*(self.feed_angle[ant]+self.parallactic_angle[ant,:])) # INI: opposite of feed angle i.e. parang +/- elev
            self.pjones_mat[ant,:,1,1] = np.exp(1j*(self.feed_angle[ant]+self.parallactic_angle[ant,:]))
          elif self.mount[ant] == 'ALT-AZ+NASMYTH-L':
            self.pjones_mat[ant,:,0,0] = np.exp(-1j*(self.feed_angle[ant]+self.parallactic_angle[ant,:]-self.elevation_copy_dterms[ant,:]))
            self.pjones_mat[ant,:,1,1] = np.exp(1j*(self.feed_angle[ant]+self.parallactic_angle[ant,:]-self.elevation_copy_dterms[ant,:]))
          elif self.mount[ant] == 'ALT-AZ+NASMYTH-R':
            self.pjones_mat[ant,:,0,0] = np.exp(-1j*(self.feed_angle[ant]+self.parallactic_angle[ant,:]+self.elevation_copy_dterms[ant,:]))
            self.pjones_mat[ant,:,1,1] = np.exp(1j*(self.feed_angle[ant]+self.parallactic_angle[ant,:]+self.elevation_copy_dterms[ant,:]))
          
        data_reshaped = self.data.reshape((self.data.shape[0],self.data.shape[1],2,2))

        for a0 in range(self.Nant):
            for a1 in range(a0+1,self.Nant):
                bl_ind = self.baseline_dict[(a0,a1)]
                for ind in bl_ind:
                  for freq_ind in range(self.num_chan):
                    data_reshaped[ind,freq_ind] = np.matmul(self.djones_mat[a0,freq_ind], np.matmul(self.pjones_mat[a0,time_ind], np.matmul(data_reshaped[ind,freq_ind], \
                                         np.matmul(np.conjugate(self.pjones_mat[a1,time_ind].T), np.conjugate(self.djones_mat[a1,freq_ind].T)))))

        self.data = data_reshaped.reshape(self.data.shape) 
        self.save_data()

        np.save(II('$OUTDIR')+'/pjones_noparangcorr_timestamp_%d'%(self.timestamp), self.pjones_mat)
        np.save(II('$OUTDIR')+'/djones_noparangcorr_timestamp_%d'%(self.timestamp), self.djones_mat)

      elif self.parang_corrected == True:
        # INI: Remove parallactic angle rotation effect (vis in sky plane). Hence, perform 2*field_angle rotation (Leppanen, 1995)
        info("Applying D-terms with parang rotation corrected for. Visibilities are in the sky plane.")

        # Construct station-based leakage matrices (D-Jones)
        #self.pol_leak_mat = np.zeros((self.Nant,2,2),dtype=complex) # To serve as both D_N and D_C
        #self.pol_leak_mat = np.zeros((self.Nant,self.time_unique.shape[0],2,2),dtype=complex)
        self.pol_leak_mat = np.ones((self.Nant,self.time_unique.shape[0],self.num_chan,2,2),dtype=complex)
        self.djones_mat = np.ones((self.Nant,self.num_chan,2,2),dtype=complex)

        for ant in range(self.Nant):
          self.djones_mat[ant,:,0,1] = self.rng_predict.normal(self.dR_mean.real[ant],self.dR_std.real[ant],size=(self.num_chan)) + 1j*self.rng_predict.normal(self.dR_mean.imag[ant],self.dR_std.imag[ant],size=(self.num_chan))
          self.djones_mat[ant,:,1,0] = self.rng_predict.normal(self.dL_mean.real[ant],self.dL_std.real[ant],size=(self.num_chan)) + 1j*self.rng_predict.normal(self.dL_mean.imag[ant],self.dL_std.imag[ant],size=(self.num_chan))

        # Set up D = D_N = D_C, Rot(theta = parallactic_angle +/- elevation). Notation following Dodson 2005, 2007.
        for ant in range(self.Nant):
          for freq_ind in range(self.num_chan):
            if self.mount[ant] == 'ALT-AZ':
                self.pol_leak_mat[ant,:,freq_ind,0,1] = self.djones_mat[ant,freq_ind,0,1] * np.exp(1j*2*(self.feed_angle[ant]+self.parallactic_angle[ant,:]))
                self.pol_leak_mat[ant,:,freq_ind,1,0] = self.djones_mat[ant,freq_ind,1,0] * np.exp(-1j*2*(self.feed_angle[ant]+self.parallactic_angle[ant,:]))

            elif self.mount[ant] == 'ALT-AZ+NASMYTH-L':
                self.pol_leak_mat[ant,:,freq_ind,0,1] = self.djones_mat[ant,freq_ind,0,1] * np.exp(1j*2*(self.feed_angle[ant]+self.parallactic_angle[ant,:]-self.elevation_copy_dterms[ant,:]))
                self.pol_leak_mat[ant,:,freq_ind,1,0] = self.djones_mat[ant,freq_ind,1,0] * np.exp(-1j*2*(self.feed_angle[ant]+self.parallactic_angle[ant,:]-self.elevation_copy_dterms[ant,:]))
           
            elif self.mount[ant] == 'ALT-AZ+NASMYTH-R':
                self.pol_leak_mat[ant,:,freq_ind,0,1] = self.djones_mat[ant,freq_ind,0,1] * np.exp(1j*2*(self.feed_angle[ant]+self.parallactic_angle[ant,:]+self.elevation_copy_dterms[ant,:]))
                self.pol_leak_mat[ant,:,freq_ind,1,0] = self.djones_mat[ant,freq_ind,1,0] * np.exp(-1j*2*(self.feed_angle[ant]+self.parallactic_angle[ant,:]+self.elevation_copy_dterms[ant,:]))

        # Save to external file as numpy array
        np.save(II('$OUTDIR')+'/panddjones_parangcorr_timestamp_%d'%(self.timestamp), self.pol_leak_mat)
        np.save(II('$OUTDIR')+'/dterms_parangcorr_timestamp_%d'%(self.timestamp), self.djones_mat)

        data_reshaped = self.data.reshape((self.data.shape[0],self.data.shape[1],2,2))

        for a0 in range(self.Nant):
            for a1 in range(a0+1,self.Nant):
                bl_ind = self.baseline_dict[(a0,a1)]
                time_ind = 0
                for ind in bl_ind:
                  for freq_ind in range(self.num_chan):
                    data_reshaped[ind,freq_ind] = np.matmul(self.pol_leak_mat[a0,time_ind,freq_ind], np.matmul(data_reshaped[ind,freq_ind], \
                                         np.conjugate(self.pol_leak_mat[a1,time_ind,freq_ind].T)))
                  time_ind = time_ind + 1
                
        self.data = data_reshaped.reshape(self.data.shape) 
        self.save_data()


    def make_pol_plots(self):
        ### parang vs time ###
        pl.figure(figsize=(10,6.8))
        for ant in range(self.Nant):
        #for ant in [0,3,6,8]:
            if (self.station_names[ant] == 'JCMT') or (self.station_names[ant] == 'JC') or\
               (self.station_names[ant] == 'ALMA') or (self.station_names[ant] == 'AA'):
                ls=''
                alpha=1
                lw=2
                #zorder=2
                marker='+'
            else:
                ls=''
                alpha=1
                lw=2
                #zorder=2
                marker='.'
            pl.plot(np.linspace(0,self.obslength,len(self.time_unique))/(60*60.),
                    self.parallactic_angle[ant, :]*180./np.pi, alpha=alpha, lw=lw,\
                    ls=ls, label=self.station_names[ant], marker=marker)
        pl.xlabel('Relative time / hr', fontsize=FSIZE)
        pl.ylabel('Parallactic angle / degrees', fontsize=FSIZE)
        pl.xticks(fontsize=20)
        pl.yticks(fontsize=20)
        #lgd = pl.legend(bbox_to_anchor=(1.02,1),loc=2,shadow=True)
        lgd = pl.legend(loc='upper left', prop={'size':12})
        pl.savefig(os.path.join(v.PLOTDIR,'parallactic_angle_vs_time.png'),\
                   bbox_extra_artists=(lgd,), bbox_inches='tight')

    ##################################
    # COMPLEX G-JONES FUNCTIONS #
    ##################################

    def add_gjones_manual(self):
        """ Add station-based complex gains """

        self.gain_mat = np.zeros((self.Nant,self.time_unique.shape[0],2,2),dtype=complex)
        for ant in range(self.Nant):
            self.gain_mat[ant,:,0,0] = self.rng_predict.normal(self.gR_mean.real[ant], self.gR_std.real[ant], size=(self.time_unique.shape[0])) + 1j*self.rng_predict.normal(self.gR_mean.imag[ant], self.gR_std.imag[ant], size=(self.time_unique.shape[0]))
            self.gain_mat[ant,:,1,1] = self.rng_predict.normal(self.gL_mean.real[ant], self.gL_std.real[ant], size=(self.time_unique.shape[0])) + 1j*self.rng_predict.normal(self.gL_mean.imag[ant], self.gL_std.imag[ant], size=(self.time_unique.shape[0]))

        np.save(II('$OUTDIR')+'/gterms_timestamp_%d'%(self.timestamp), self.gain_mat) # INI: Add timestamps to the output gain files so that SYMBA has access to them.

        data_reshaped = self.data.reshape((self.data.shape[0],self.data.shape[1],2,2))

        for a0 in range(self.Nant):
            for a1 in range(a0+1,self.Nant):
                bl_ind = self.baseline_dict[(a0,a1)]
                utime=0 # INI: the assumption here is that all baselines have an entry for each utime
                for ind in bl_ind:
                  data_reshaped[ind] = np.matmul(np.matmul(self.gain_mat[a0,utime], data_reshaped[ind]), np.conjugate(self.gain_mat[a1,utime].T))
                  utime = utime + 1

        self.data = data_reshaped.reshape(self.data.shape)

        self.save_data()

    ##################################
    # Add noise components
    def add_noise(self, tropnoise, thermalnoise):
        """ Add sky and receiver noise components to the visibilities and populate weight columns"""

        # compute receiver rms noise
        for a0 in range(self.Nant):
            for a1 in range(self.Nant):
                if a1 > a0:
                    self.receiver_rms[self.baseline_dict[(a0,a1)]] = (1/self.corr_eff) * np.sqrt(self.SEFD[a0] * \
                                                                        self.SEFD[a1] / float(2 * self.tint * self.chan_width))

        # compute sky sigma estimator (i.e. sky rms noise) and realise sky noise
        if tropnoise:
            sefd_matrix = 2 * Boltzmann / self.dish_area * (1e26*self.sky_temp * (1. - np.exp(-1.0 * self.opacity / np.sin(self.elevation_tropshape))))
            self.sky_noise = np.zeros(self.data.shape, dtype='complex')
            sky_sigma_estimator = np.zeros(self.data.shape)

            for a0 in range(self.Nant):
                for a1 in range(self.Nant):
                    if a1 > a0:
                        rms = (1/self.corr_eff) * np.sqrt(sefd_matrix[:, :, a0] * sefd_matrix[:, :, a1] / (float(2 * self.tint * self.chan_width)))
                        self.temp_rms = rms
                        rms = np.expand_dims(rms, 2)
                        rms = rms * np.ones((1, 1, 4))
                        self.sky_noise[self.baseline_dict[(a0, a1)]] = self.rng_atm.normal(0.0, rms) + 1j * self.rng_atm.normal(0.0, rms)
                        sky_sigma_estimator[self.baseline_dict[(a0, a1)]] = rms
            np.save(II('$OUTDIR')+'/atm_output/sky_noise_timestamp_%d'%(self.timestamp), self.sky_noise)

            # add sky noise rms to receiver rms if tropnoise is set
            try:
              for tind in range(self.nchunks):
                self.receiver_rms[tind*self.chunksize:(tind+1)*self.chunksize] = np.sqrt(np.power(self.receiver_rms[tind*self.chunksize:(tind+1)*self.chunksize], 2) + \
                                                                               np.power(sky_sigma_estimator[tind*self.chunksize:(tind+1)*self.chunksize], 2))
            except MemoryError:
              abort("Arrays too large to be held in memory. Aborting execution.")

        # use the receiver rms to realise thermal noise
        if thermalnoise:
            info('Realise thermal noise from receiver rms...')
            self.thermal_noise = np.zeros(self.data.shape, dtype='complex')
            size = (self.time_unique.shape[0], self.chan_freq.shape[0], 4)
            for a0 in range(self.Nant):
                for a1 in range(self.Nant):
                    if a1 > a0:
                        rms = self.receiver_rms[self.baseline_dict[(a0,a1)]]
                        self.thermal_noise[self.baseline_dict[(a0, a1)]] = self.rng_predict.normal(0.0, rms, size=size) + 1j * self.rng_predict.normal(0.0, rms, size=size)

            np.save(II('$OUTDIR')+'/receiver_noise_timestamp_%d'%(self.timestamp), self.thermal_noise)

        # add both the noise components to the data
        try:
          info('Applying thermal noise to data...')
          for tind in range(self.nchunks):
            self.data[tind*self.chunksize:(tind+1)*self.chunksize] += self.thermal_noise[tind*self.chunksize:(tind+1)*self.chunksize] + \
                                                                        self.sky_noise[tind*self.chunksize:(tind+1)*self.chunksize]
          self.save_data()
        except MemoryError:
          abort("Arrays too large to be held in memory. Aborting execution.")

        # populate MS weight and sigma columns using the receiver rms (with or without sky noise)
        tab = pt.table(self.msname, readonly=False,ack=False)
        tab.putcol("SIGMA", self.receiver_rms[:,0,:])
        if 'SIGMA_SPECTRUM' in tab.colnames():
            tab.putcol("SIGMA_SPECTRUM", self.receiver_rms)
        tab.putcol("WEIGHT", 1/self.receiver_rms[:,0,:]**2)
        if 'WEIGHT_SPECTRUM' in tab.colnames():
            tab.putcol("WEIGHT_SPECTRUM", 1/self.receiver_rms**2)
        tab.close()

    #############################
    ##### General MS plots  #####
    #############################

    def make_ms_plots(self):
        """uv-coverage, uv-dist sensitivty bins, etc. All by baseline colour"""
        info('making MS inspection plots')

        ### uv-coverage plot, different color baselines, legend, uv-annuli ###
        pl.figure(figsize=(16,16))
        #from mpltools import color
        #cmap = pl.cm.Set1
        #color.cycle_cmap(self.Nant, cmap=cmap) # INI: deprecated; use prop_cycle
        colors = [pl.cm.Set1(i) for i in np.linspace(0, 1, int(self.nbl))]
        fig, ax = pl.subplots()
        ax.set_prop_cycle(cycler('color', colors))
        for ant0 in range(self.Nant):
            for ant1 in range(self.Nant):
                if (ant1 > ant0) \
                        and not ((self.station_names[ant0]=='JCMT') or (self.station_names[ant1] == 'JCMT')) \
                        and not ((self.station_names[ant0]=='APEX') or (self.station_names[ant1] == 'APEX')):

                    temp_mask = np.logical_not(self.flag[self.baseline_dict[(ant0,ant1)],0,0])
                    temp_u = self.uvw[self.baseline_dict[(ant0,ant1)][temp_mask], 0]\
                         / (speed_of_light/self.chan_freq.mean())/1e9
                    temp_v = self.uvw[self.baseline_dict[(ant0,ant1)][temp_mask], 1]\
                         / (speed_of_light/self.chan_freq.mean())/1e9
                    #if (np.sqrt((temp_u.max()**2 + temp_v.max()**2)) > 0.1):
                    pl.plot(np.hstack([np.nan, temp_u,np.nan, -temp_u, np.nan]), np.hstack([np.nan, temp_v,np.nan, -temp_v,np.nan]), \
                            lw=2.5,label='%s-%s'%(self.station_names[ant0],self.station_names[ant1]))
            #pl.plot(-self.uvw[np.logical_not(self.flag[:, 0, 0]), 0], -self.uvw[np.logical_not(self.flag[:, 0, 0]), 1], \
            #        label=self.station_names[i])
        lgd = pl.legend(bbox_to_anchor=(1.02, 1), loc=2, shadow=True,fontsize='small')
        ax = pl.gca()

        uvbins_edges = np.arange(0, 11, 1)  # uvdistance units: Giga-lambda
        uvbins_centre = (uvbins_edges[:-1] + uvbins_edges[1:]) / 2.
        numuvbins = len(uvbins_centre)
        binwidths = uvbins_edges[1] - uvbins_edges[0]
        '''for b in range(numuvbins):
            p = Circle((0, 0), uvbins_edges[b + 1], edgecolor='k', ls='solid', facecolor='none', alpha=0.5, lw=0.5)
            ax.add_artist(p)'''
        pl.xlabel('$u$ / G$\,\lambda$', fontsize=FSIZE)
        pl.ylabel('$v$ / G$\,\lambda$', fontsize=FSIZE)
        pl.xticks(fontsize=10)
        pl.yticks(fontsize=10)        
        pl.xlim(-10, 10)
        pl.ylim(-10, 10)
        ax.set_aspect('equal')
        pl.savefig(os.path.join(v.PLOTDIR, 'uv-coverage_legend.png'), \
                   bbox_extra_artists=(lgd,), bbox_inches='tight')

        ### uv-coverage plot, colorize by minimun elevation, uv-annuli ###
        self.calculate_baseline_min_elevation() # calc min elevation in the two e for every baseline and every timestep
        self.calculate_baseline_mean_elevation()# as above, but for mean

        pl.figure(figsize=(16,16))
        #from mpltools import color
        cmap = pl.cm.Set1
        #color.cycle_cmap(self.Nant, cmap=cmap)
        fig, ax = pl.subplots()
        #temp_elevation = self.elevation.copy()
        #temp_elevation[np.isnan(temp_elevation)] = 1000.
        #elevation_mask = temp_elevation < 90.
        # converted from nan and set arbitrarily high
        for ant0 in range(self.Nant):
            for ant1 in range(self.Nant):
                if (ant1 > ant0) \
                        and not ((self.station_names[ant0]=='JCMT') or (self.station_names[ant1] == 'JCMT')) \
                        and not ((self.station_names[ant0]=='APEX') or (self.station_names[ant1] == 'APEX')):
                    temp_mask = np.logical_not(self.flag[self.baseline_dict[(ant0,ant1)],0,0])
                    self.temp_u = self.uvw[self.baseline_dict[(ant0,ant1)][temp_mask], 0]\
                         / (speed_of_light/self.chan_freq.mean())/1e9
                    self.temp_v = self.uvw[self.baseline_dict[(ant0,ant1)][temp_mask], 1]\
                         / (speed_of_light/self.chan_freq.mean())/1e9
                    temp_minelev = self.baseline_min_elevation[self.baseline_dict[(ant0,ant1)][temp_mask]]

                    pl.scatter(np.hstack([self.temp_u, -self.temp_u]), np.hstack([self.temp_v, -self.temp_v]), \
                            c=np.hstack([temp_minelev,temp_minelev])*180./np.pi,\
                               s=10,cmap="viridis",edgecolors="None",vmin=0,vmax=30) #
        cb = pl.colorbar()
        cb.set_label("Minimum baseline elevation / degrees")
        ax = pl.gca()
        for b in range(numuvbins):
            p = Circle((0, 0), uvbins_edges[b + 1], edgecolor='k', ls='solid', facecolor='none', alpha=0.5, lw=0.5)
            ax.add_artist(p)
        pl.xlabel('$u$ /  G$\,\lambda$', fontsize=FSIZE)
        pl.ylabel('$v$ /  G$\,\lambda$', fontsize=FSIZE)
        pl.xlim(-10, 10)
        pl.ylim(-10, 10)
        ax.set_aspect('equal')
        pl.savefig(os.path.join(v.PLOTDIR, 'uv-coverage_colorize_min_elevation.png'), \
                    bbox_inches='tight')

        pl.figure(figsize=(16,16))
        #from mpltools import color
        cmap = pl.cm.Set1
        #color.cycle_cmap(self.Nant, cmap=cmap)
        fig, ax = pl.subplots()
        #temp_elevation = self.elevation.copy()
        #temp_elevation[np.isnan(temp_elevation)] = 1000.
        #elevation_mask = temp_elevation < 90.
        # converted from nan and set arbitrarily high
        for ant0 in range(self.Nant):
            for ant1 in range(self.Nant):
                if (ant1 > ant0) \
                        and not ((self.station_names[ant0]=='JCMT') or (self.station_names[ant1] == 'JCMT')) \
                        and not ((self.station_names[ant0]=='APEX') or (self.station_names[ant1] == 'APEX')):
                    temp_mask = np.logical_not(self.flag[self.baseline_dict[(ant0,ant1)],0,0])
                    self.temp_u = self.uvw[self.baseline_dict[(ant0,ant1)][temp_mask], 0]\
                         / (speed_of_light/self.chan_freq.mean())/1e9
                    self.temp_v = self.uvw[self.baseline_dict[(ant0,ant1)][temp_mask], 1]\
                         / (speed_of_light/self.chan_freq.mean())/1e9
                    temp_meanelev = self.baseline_mean_elevation[self.baseline_dict[(ant0,ant1)][temp_mask]]

                    pl.scatter(np.hstack([self.temp_u, -self.temp_u]), np.hstack([self.temp_v, -self.temp_v]), \
                            c=np.hstack([temp_meanelev,temp_meanelev])*180./np.pi,\
                               s=10,cmap="viridis",edgecolors="None",vmin=0,vmax=30) #
        cb = pl.colorbar()
        cb.set_label("Mean baseline elevation / degrees")
        ax = pl.gca()
        for b in range(numuvbins):
            p = Circle((0, 0), uvbins_edges[b + 1], edgecolor='k', ls='solid', facecolor='none', alpha=0.5, lw=0.5)
            ax.add_artist(p)
        pl.xlabel('$u$ /  G$\,\lambda$', fontsize=FSIZE)
        pl.ylabel('$v$ /  G$\,\lambda$', fontsize=FSIZE)
        pl.xlim(-10, 10)
        pl.ylim(-10, 10)
        ax.set_aspect('equal')
        pl.savefig(os.path.join(v.PLOTDIR, 'uv-coverage_colorize_mean_elevation.png'), \
                    bbox_inches='tight')

        ampbins = np.zeros([numuvbins])
        stdbins = np.zeros([numuvbins])
        phasebins = np.zeros([numuvbins])
        phstdbins = np.zeros([numuvbins])
        Nvisperbin = np.zeros([numuvbins])
        corrs = [0,3] # only doing Stokes I for now

        for b in range(numuvbins):
            mask = ( (self.uvdist / (speed_of_light/self.chan_freq.mean())/1e9) > uvbins_edges[b]) & \
                   ( (self.uvdist / (speed_of_light/self.chan_freq.mean())/1e9) < uvbins_edges[b + 1]) & \
                   (np.logical_not(self.flag[:, 0, 0]))  # mask of unflagged visibilities in this uvbin
            Nvisperbin[b] = mask.sum()  # total number of visibilities in this uvbin
            ampbins[b] = np.nanmean(abs(self.data[mask, :, :])[:, :, corrs])  # average amplitude in bin "b"
            #stdbins[b] = np.nanstd(abs(self.data[mask, :, :])[:, :, corrs]) / Nvisperbin[b]**0.5  # rms of that bin

            if (self.trop_enabled) and (self.thermal_noise_enabled):
                stdbins[b] = np.nanmean(abs(np.add(self.thermal_noise[mask, :, :][:, :, corrs], \
                                                   self.sky_noise[mask, :, :][:, :, corrs]))) / Nvisperbin[b] ** 0.5
            elif (not self.trop_enabled) and (self.thermal_noise_enabled):
                stdbins[b] = np.nanmean(abs(self.thermal_noise[mask, :, :][:, :, corrs])) \
                                        / Nvisperbin[b] ** 0.5
            elif (self.trop_enabled) and (not self.thermal_noise_enabled):
                if self.sky_noise is not None:
                    stdbins[b] = np.nanmean(abs(self.sky_noise[mask, :, :][:, :, corrs])) \
                                            / Nvisperbin[b] ** 0.5
            else:
                stdbins[b] = np.nanstd(abs(self.data[mask, :, :])[:, :, corrs]) / Nvisperbin[b]**0.5  # rms of that bin
            # next few lines if a comparison array is desired (e.g. EHT minus ALMA)
            #mask_minus1ant = (uvdist > uvbins_edges[b])&(uvdist< uvbins_edges[b+1])&(np.logical_not(flag_col[:,0,0]))& \
            # (ant1 != station_name.index('ALMA'))&(ant2 != station_name.index('ALMA'))
            # mask of unflagged visibilities in this uvbin, that don't include any ALMA baselines
            #Nvisperbin_minus1ant[b] = mask_nomk.sum()  # total number of visibilities in this uvbin
            #ampbins_minus1ant[b] = np.nanmean(abs(data[mask_nomk, :, :])[:, :, corrs])  # average amplitude in bin "b"
            #stdbins_minus1ant[b] = np.nanstd(abs(data[mask_nomk, :, :])[:, :, corrs]) / Nvisperbin_nomk[b] ** 0.5  # rms of that bin

            phasebins[b] = np.nanmean(np.arctan2(self.data[mask, :, :].imag, \
                                                 self.data[mask, :, :].real)[:, :,
                                      corrs])  # average phase in bin "b"
            phstdbins[b] = np.nanstd(np.arctan2(self.data[mask, :, :].imag, \
                                                self.data[mask, :, :].real)[:, :, corrs])  # rms of that bin

        phasebins *= (180 / np.pi)
        phstdbins *= (180 / np.pi)  # rad2deg

        def uvdist2uas(uvd):
            theta = 1. / (uvd * 1e9) * 206265 * 1e6  # Giga-lambda to uas
            return ["%.1f" % z for z in theta]

        def uas2uvdist(ang):
            return 1. / (ang / (206265. * 1e6)) / 1e9

        ### this is for a top x-axis labels, showing corresponding angular scale for a uv-distance
        angular_tick_locations = [25, 50, 100, 200]  # specify which uvdist locations you want a angular scale

        ### amp vs uvdist, with uncertainties
        fig = pl.figure(figsize=(10,6.8))
        ax1 = fig.add_subplot(111)
        ax2 = ax1.twiny()
        yerr = stdbins/np.sqrt(Nvisperbin) #noise_per_vis/np.sqrt(np.sum(Nvisperbin,axis=0)) #yerr = noise_per_vis/np.sqrt(np.sum(allsrcs[:,2,:],axis=0))
        xerr = binwidths/2. * np.ones(numuvbins)
        for b in range(numuvbins):
            ax1.plot(uvbins_centre[b],ampbins[b],'o',mec='none',alpha=1,color='#336699')
            ax1.errorbar(uvbins_centre[b],ampbins[b],xerr=xerr[b],yerr=yerr[b],ecolor='grey',lw=0.5,alpha=1,fmt='none',capsize=0)
        #ax1.vlines(uas2uvdist(shadow_size_mas),0,np.nanmax(ampbins)*1.2,linestyles='dashed')
        ax1.set_xlabel('${uv}$-distance / G$\,\lambda$', fontsize=FSIZE)
        ax1.set_ylabel('Stokes I amplitude / Jy', fontsize=FSIZE)
        ax1.set_ylim(0,np.nanmax(ampbins)*1.2)
        ax1.set_xlim(0,uvbins_edges.max())
        ax2.set_xlim(ax1.get_xlim())

        # configure upper x-axis

        ax2.set_xticks(uas2uvdist(np.array(angular_tick_locations))) # np.array([25.,50.,100.,200.]))) #   angular_tick_locations))
        ax2.set_xticklabels(angular_tick_locations)
        #ax2.xaxis.set_major_formatter(FormatStrFormatter('%i'))
        ax2.set_xlabel("Angular scale / $\mu$-arcsec", fontsize=FSIZE)
        #np.savetxt('uvdistplot_ampdatapts.txt',np.vstack([uvbins_centre,xerr,ampbins,yerr]))
        pl.savefig(os.path.join(v.PLOTDIR,'amp_uvdist.png'), \
                   bbox_inches='tight')

        ### percent of visibilties per bin
        percentVisperbin = Nvisperbin/Nvisperbin.sum()*100
        #percentVisperbin_minus1ant = Nvisperbin_minus1ant/Nvisperbin_minus1ant.sum()*100
        #percent_increase = (Nvisperbin/Nvisperbin_minus1ant -1) * 100

        fig = pl.figure(figsize=(10,6.8))
        ax1 = fig.add_subplot(111)
        ax2 = ax1.twiny()
        for b in range(numuvbins):
            #ax1.bar(uvbins_centre[b],percent_increase[b],width=binwidths,color='orange',alpha=1) #,label='MeerKAT included')
            ax1.bar(uvbins_centre[b],percentVisperbin[b],width=binwidths,color='orange',alpha=0.9,align='center',edgecolor='none') #,label='')
            #ax1.bar(uvbins_centre[b],percentVisperbin_minus1ant[b],width=binwidths,color='#336699',alpha=0.6,label='MeerKAT excluded')
        ax1.set_xlabel('$uv$-distance / G$\,\lambda$', fontsize=FSIZE)
        ax1.set_ylabel('Percentage of total visibilities', fontsize=FSIZE)
        #ax1.set_ylabel('percentage increase')
        #ax1.set_ylim(0,np.nanmax(percentVisperbin)*1.2)
        #ax1.set_ylim(0,percent_increase.max()*1.2)
        ax1.set_xlim(0,uvbins_edges.max())
        #ax1.vlines(uas2uvdist(shadow_size_uarcsec),0,np.nanmax(Nvisperbin)*1.2,linestyles='dashed')
        ax2.set_xlim(ax1.get_xlim())
        # configure upper x-axis
        ax2.set_xticks(uas2uvdist(np.array(angular_tick_locations)))
        ax2.set_xticklabels(angular_tick_locations) #(angular_tick_locations))
        ax2.set_xlabel(r"Angular scale / $\mu$-arcsec", fontsize=FSIZE)
        #pl.legend()
        pl.savefig(os.path.join(v.PLOTDIR,'num_vis_perbin.png'), \
                   bbox_inches='tight')

        ### averaged sensitivity per bin
        fig = pl.figure(figsize=(10,6.8))
        ax1 = fig.add_subplot(111)
        ax2 = ax1.twiny()
        #x_vlba,y_vlba = np.loadtxt('/home/deane/git-repos/vlbi-sim/output/XMM-LSS/vlba_xmmlss_sigma_vs_uvbin.txt').T #/home/deane/git-repos/vlbi-sim/output/VLBA_COSMOS/vlba_sigma_vs_uvbin.txt',comments='#').T
        x = np.ravel(list(zip(uvbins_edges[:-1],uvbins_edges[1:])))
        y = np.ravel(list(zip(stdbins,stdbins)))
        #y_minus1ant = np.ravel(zip(stdbins_minus1ant,stdbins_minus1ant))

        #ax1.plot(x_vlba,y_vlba*1e6,color='grey',alpha=1,label='VLBA',lw=3)
        ax1.plot(x,y*1e3,color='#336699',linestyle='solid',alpha=1,label='EHT',lw=3)
        #ax1.plot(x,y*1e6,color='orange',alpha=0.7,label='EVN + MeerKAT',lw=3)

        ax1.set_xlabel('$uv$-distance / G$\,\lambda$', fontsize=18)
        ax1.set_ylabel('Thermal + sky noise rms / mJy', fontsize=18)
        #ax1.set_ylabel('percentage increase')
        ax1.set_ylim(0,np.nanmax(y)*1.2*1e3)
        ax1.set_xlim(0,uvbins_edges.max())
        ax1.tick_params(axis='both', which='major', labelsize=12)
        #ax1.vlines(uas2uvdist(shadow_size_uarcsec),0,np.nanmax(Nvisperbin)*1.2,linestyles='dashed')
        ax2.set_xlim(ax1.get_xlim())
        # configure upper x-axis
        ax2.set_xticks(uas2uvdist(np.array(angular_tick_locations)))
        ax2.set_xticklabels(angular_tick_locations)
        ax2.set_xlabel(r"angular scale / $\mu$-arcsec", fontsize=FSIZE)
        ax2.tick_params(axis='both', which='major', labelsize=12)
        #ax1.legend(loc='upper left',fontsize=16)
        pl.savefig(os.path.join(v.PLOTDIR, 'sensitivity_perbin.png'), \
             bbox_inches = 'tight')

        ### elevation vs time ###
        pl.figure(figsize=(10,6.8))
        for ant in range(self.Nant):
            if (self.station_names[ant] == 'JCMT') or \
               (self.station_names[ant] == 'APEX'):
                ls = ':'
                lw=3.5
                alpha = 1
                zorder = 2
            else:
                ls = 'solid'
                alpha = 1
                lw=2
                zorder = 1
            pl.plot(np.linspace(0,self.obslength,len(self.time_unique))/(60*60.),
                    self.elevation[ant, :]*180./np.pi, alpha=alpha, lw=lw, \
                    ls=ls,zorder=zorder,label=self.station_names[ant])
        pl.xlabel('Relative time / hr', fontsize=FSIZE)
        pl.ylabel('Elevation / degrees', fontsize=FSIZE)
        lgd = pl.legend(bbox_to_anchor=(1.02,1),loc=2,shadow=True)
        pl.savefig(os.path.join(v.PLOTDIR,'antenna_elevation_vs_time.png'),\
                   bbox_extra_artists=(lgd,), bbox_inches='tight')


