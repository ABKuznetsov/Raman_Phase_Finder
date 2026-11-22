# Raman Phase Finder

Open-source desktop software for Raman spectroscopy: phase identification, search-match against reference libraries, spectrum processing, and publication-ready figures.

## Download and run

- **macOS:** [Download Raman Phase Finder 1.0.0](https://github.com/ABKuznetsov/Raman_Phase_Finder/releases/tag/v1.0.0). Choose the `.pkg` installer under Assets. The launcher prepares the Python environment on first run.
- **Windows and Linux:** use the [source distribution](https://github.com/ABKuznetsov/Raman_Phase_Finder). Run `run_finder.bat` on Windows, or use Python 3.11–3.12 and run `bash setup_env.sh` followed by `bash run_finder.sh` on Linux. The first release includes a macOS installer only.

The app checks for updates after opening. You can also check from **Help → Check for updates**. Downloads and releases are published in this repository. If migrating from an earlier installation, install the standalone release manually once. Existing Raman `.irff` projects, user libraries, cached references and preferences remain usable.

## Workflow

1. Import Raman spectra and organize them into series. Inspect single spectra or compare selected spectra together.
2. Detect bands, set optional element filters, and run **Identify compounds**. Review **Match** before accepting a phase and **Gain** for remaining bands after acceptance.
3. Compare reference profiles and lines, show vibrational modes and assignments, or inspect estimated component profiles and the residual. Search scores and fitted profiles are not quantitative phase fractions.
4. Arrange the graph and export a publication figure as PNG, JPEG, SVG or PDF.

The database manager supports Raman sources including RRUFF, ROD, OpenSpecy, and Oulu CRD, as well as local references and user-configured sources. Third-party databases and measurements are not bundled. Use **Add reference** to create or import a custom Raman card; a blank [JSON template](reference_template.json) is included.

Raman Phase Finder is developed by Artem B. Kuznetsov, Institute of Geology and Mineralogy SB RAS. The interface follows the [XRD Analysis Toolkit](https://github.com/ABKuznetsov/XRD_Analysis_Toolkit), with a separate Raman search and processing engine.

[MIT License](LICENSE)
