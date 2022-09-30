=======================
Example input JSON file
=======================

Examples for all input types can be found in the source code. The following is an example JSON file that simulates a polarised
sky model and corrupts the complex visibilities with SEFD-based thermal noise.

.. code-block:: JSON

  {
   "outdirname":"EHTsim",
   "input_fitsimage":"old_grmhd_pol",
   "input_fitspol":1,
   "input_changroups":1,
   "output_to_logfile":0,
   "add_thermal_noise":1,
   "make_image":0,
   "exportuvfits":0,
   "station_info":"eht_betterweather.antennas",
   "bandpass_enabled":0,
   "bandpass_table":"eht_bandpass.txt",
   "bandpass_freq_interp_order":1,
   "bandpass_makeplots": 0,
   "elevation_limit":0.174,
   "corr_quantbits":2,
   "predict_oversampling":8191,
   "predict_seed":42,
   "atm_seed":42,
   "ms_antenna_table":"ANTENNA_EHT2017",
   "ms_datacolumn":"DATA",
   "ms_RA":187.70591666666667,
   "ms_DEC":12.391122222222222,
   "ms _polproducts":"RR RL LR LL",
   "ms_nu":228,
   "ms_dnu":2,
   "ms_nchan":64,
   "ms_obslength":4,
   "ms_tint":10,
   "ms_StartTime":"UTC,2017/04/11/00:32:00.00",
   "ms_nscan":1,
   "ms_scan_lag":0,
   "ms_makeplots": 1,
   "ms_correctCASAoffset":1,
   "im_cellsize":"3e-6arcsec",
   "im_npix":64,
   "im_stokes":"I",
   "im_weight":"uniform",
   "trop_enabled":0,
   "trop_wetonly":0,
   "trop_attenuate":1,
   "trop_noise":1,
   "trop_turbulence":1,
   "trop_mean_delay":1,
   "trop_percentage_calibration_error":100,
   "trop_fixdelays":0,
   "trop_fixdelay_max_picosec": 0,
   "trop_makeplots": 0,
   "pointing_enabled":0,
   "pointing_time_per_mispoint": 10,
   "pointing_makeplots": 0,
   "uvjones_g_on": 0,
   "uvjones_d_on": 0,
   "parang_corrected": 1
  }
