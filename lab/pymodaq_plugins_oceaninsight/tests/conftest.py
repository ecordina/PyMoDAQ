import os

# the tests do not need any display nor any spectrometer
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
