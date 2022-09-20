=======
History
=======

2.8 (xxxx)
----------

* Add ability to read in an existing MS and regularize it
* Update tropospheric plotting modules 
* Apply tropospheric corruptions to single channel datasets

2.7.1 (2021)
------------

* Update how plotting modules handle non-existent arrays
* New tropospheric turbulence module

2.7 (2021)
----------

* Package MeqSilhouette
* Update singularity recipe and Dockerfile
* Update casa data while building images
* Update documentation

2.6.2 (2021)
------------

* Extensive updates to documentation
* Singularity containerisation
* Add option to run with Jupyter notebook
* Add license
* Update ms plotting module
* Ensure uncorrupted vis are copied to MODEL_DATA always

2.6.1 (2021)
------------

* Improve output path handling
* Synchronize sample input files and default input settings
* Update documentation

2.6 (2021)
----------

* Implement frequency-dependent polarization leakage and remove time dependence
* Improve error handling for memory errors
* Chunk data to fit in memory
* Add paper-friendly plots

2.5 (2020)
----------

* Generate real and imaginary parts of orthogonal polarization feeds independently for time-varying antenna gains and polarization leakage
* Generate interpolated bandpass gains independently for orthogonal polarization feeds
* Clean up input files

2.4 (2020)
----------

* Handle frequency-dependent source models
* Verify existence of bandpass gains table
* Add new bandpass plotting capability
* Implement time-varying complex antenna gains
* Remove deprecated functions

2.3 (2020)
----------

* Streamline random seed initialization
* Handle potential rounding errors in antenna pointing offsets
* Add sky noise to visibility weight estimation
* Include CASA time offset correction

2.0 (2019)
----------

* Depend mainly on WSClean for forward modelling (MeqTrees only for txt sky models)
* Full polarimetric simulations
* Simulate time-variable sources
* Add complex bandpass gains
* Add instrumental polarization and parallactic angle rotation (write visibilities in both antenna and sky frames)
* Improve pointing error module
* Improve tropospheric corruption and thermal noise modules
* Add plotting modules
* Remove scattering screen
* Refactor code for seamless integration within pipelines such as SYMBA

1.0 (2016)
----------
* Tropospheric corruptions
* Basic pointing error module
* ISM scattering
