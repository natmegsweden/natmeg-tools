"""Read and return raw daata"""

from mne.io import read_raw_fif, read_raw_brainvision

def read_data(path):
    if path.suffix.lower() == ".vhdr":
        raw = read_raw_brainvision(path, preload=True)
    elif path.suffix.lower() == ".fif":
        raw = read_raw_fif(path, preload=True)
    return raw