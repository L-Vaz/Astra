import os
import sys

# Force headless rendering early
os.environ.setdefault('PYVISTA_OFF_SCREEN', 'true')

import pyvista as pv

try:
    pv.OFF_SCREEN = True
except Exception:
    pass

try:
    pv.start_xvfb()
except Exception:
    # start_xvfb is optional and may be unavailable
    pass

foam_path = os.path.abspath('run2.foam')
out_png = os.path.abspath('visualization.png')

reader = pv.OpenFOAMReader(foam_path)
# Many OpenFOAM readers expose available times; use the last one when present
try:
    reader.set_active_time_value(reader.time_values[-1])
except Exception:
    pass

data = reader.read()

# data can be a MultiBlock; merge to a single mesh for robust plotting
mesh = data
try:
    if hasattr(data, 'combine'):
        mesh = data.combine()
except Exception:
    # fallback: try first block
    try:
        mesh = data[0]
    except Exception:
        mesh = data

# Determine a scalar to plot
scalar_name = None
preferred = 'p'

# Try point data first then cell data
try:
    if preferred in getattr(mesh, 'point_data', {}):
        scalar_name = preferred
    elif preferred in getattr(mesh, 'cell_data', {}):
        scalar_name = preferred
except Exception:
    pass

if scalar_name is None:
    # pick any available scalar
    try:
        keys = list(getattr(mesh, 'point_data', {}).keys())
        scalar_name = keys[0] if keys else None
    except Exception:
        scalar_name = None

if scalar_name is None:
    try:
        keys = list(getattr(mesh, 'cell_data', {}).keys())
        scalar_name = keys[0] if keys else None
    except Exception:
        scalar_name = None

plotter = pv.Plotter(off_screen=True)
plotter.set_background('white')

if scalar_name is not None:
    plotter.add_mesh(mesh, scalars=scalar_name, cmap='coolwarm', show_scalar_bar=True)
else:
    plotter.add_mesh(mesh, color='lightgray')

plotter.view_isometric()
plotter.show(auto_close=False)
plotter.screenshot(out_png)
plotter.close()

print('Wrote', out_png)
