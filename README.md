# NATMEG Tools

This repository contains optional research tools for OPM-MEG, EEG-fMRI [under development], and a local Lab Notebook. The root project is a lightweight dependency bundle: install only the dependencies for the components you need.

## Requirements

- Python 3.11 or later
- Git, including submodule support for the OPM utilities

Clone the repository and initialize its OPM submodule:

```sh
git clone --recurse-submodules <repository-url>
cd natmeg-tools
```

If the repository was cloned without submodules, run:

```sh
git submodule update --init --recursive
```

## Install Optional Dependencies

Install one component's dependencies from the repository root. `uv sync` creates or updates `.venv` and uses the checked-in `uv.lock`:

```sh
uv sync --extra opm # submodule to natmegsweden/opm_utility_scripts
uv sync --extra lab-notebook
uv sync --extra eeg # Under development
uv sync --extra tutorials # Under development
```

Install multiple dependency groups together by listing their extras:

```sh
uv sync --extra opm --extra eeg --extra tutorials
```

Available groups:

- `opm`: core OPM dependencies
- `opm-tools`: OPM core plus Pandas-based tools
- `opm-vtk`: OPM core plus Pandas, PyQt5, and VTK tools
- `lab-notebook`: Streamlit and MNE for the local notebook app
- `eeg`: MNE, NumPy, and SciPy for EEG-fMRI utilities
- `tutorials`: Jupyter and EEG dependencies for Python-backed tutorials

The root extras install dependencies for code in this checkout. To install the OPM package and its `opmutil` command itself, use its package metadata:

```sh
uv sync --project opm
uv sync --project opm --extra tools
uv sync --project opm --extra vtk
```

The Lab Notebook can also be installed independently, including its test dependency:

```sh
uv sync --project lab-notebook
uv sync --project lab-notebook --extra test
```

## Run

Start the Lab Notebook from the repository root. It binds to localhost only:

```sh
uv run streamlit run lab-notebook/app.py --server.address 127.0.0.1
```

On first launch, configure the direct `Recordings` directory and Lab export directory and create the shared admin password in the browser. App data defaults to `$XDG_DATA_HOME/lab-notebook` or `~/.local/share/lab-notebook` on Linux; override it with `LAB_NOTEBOOK_DATA_DIR`. See `lab-notebook/README.md` for snapshot history, admin settings, and local password reset.

Run the OPM command after installing the OPM package:

```sh
uv run --project opm opmutil --help
```

The EEG-fMRI modules and tutorials are source files in this checkout. Run Python commands from the repository root after installing the matching extra so the source tree is importable.

The tutorials extra installs Jupyter and the Python libraries used by the tutorials. Rendering `.qmd` files also requires the Quarto command-line application, which must be installed separately. For example:

```sh
uv run --extra tutorials quarto render tutorials/eeg-fMRI-example.qmd
```

## Tests

Run the Lab Notebook tests after syncing its test extra:

```sh
uv run --project lab-notebook --extra test pytest lab-notebook/tests
```

The OPM submodule has its own test suite and package configuration; consult `opm/README.md` for its instructions.
