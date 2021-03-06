#!/usr/bin/env python

"""
Implementation of a customized cellular automaton for sandpile dynamics.
The lattice sites in the sandbox in the context of this model must be regarded as the actual heights of the sand pile.
"""

import os  # File saving etc.
import time  # Timing
import logging  # User feedback
import numpy as np  # Arrays with fast, vectorized operations and flexible structure
import argparse  # simulation setup file

from scipy.spatial.distance import cdist, pdist  # Calculate distances
from numba import njit  # Speed-up
from collections import Iterable  # Type checking
from plot_utils import plot_hist, plot_sandbox  # plotting
from tools import save_simulation, save_sandbox

logging.basicConfig(level=logging.INFO)

try:
    yaml_flag = False
    import yaml  # simulation setup file; don't require yaml if setup is selected in script
except ImportError:
    yaml_flag = True
    logging.info('Starting simulation from setup.yaml disabled. Could not import yaml.')
    
try:
    pg_flag = False
    import pyqtgraph as pg  # Plotting; don't require pg if no live plotting
    from plot_utils import SimulationPlotter  # Live plotting
except ImportError:
    pg_flag = True
    logging.info('Plotting live evolution of sandbox disabled. Could not import pyqtgraph.')


### Simulation functions ###


def init_sandbox(n, dim, state='fill', critical_slope=5):
    """
    Initialises a square NxN sandbox lattice on which the simulation is done.

    :param n: int of lattice sites of sandbox
    :param dim: int of dimension like n^dim
    :param state: str some state of the initial sandbox
    :param critical_slope: int critical height of pile of sand grains

    """

    if state == 'fill':
        res = n*critical_slope*np.ones(shape=(n, ) * dim, dtype=np.int16)
    elif state == 'one':
        res = np.ones(shape=(n, ) * dim, dtype=np.int16)
    elif state == 'crit':
        res = np.random.choice([critical_slope-1, critical_slope], shape=(n, ) * dim, dtype=np.int16)
    elif state == 'over_crit':
        res = np.random.choice([critical_slope, critical_slope + 1], shape=(n, ) * dim, dtype=np.int16)
    else:  # None, ground state
        res = np.zeros(shape=(n, ) * dim, dtype=np.int16)

    return res

@njit
def off_boundary(s, x):
    """
    Checks whether site x is beyond the boundary of the sandbox.

    :param s: np.array
    :param x: int position of grain drop-off
    """

    for i, x_i in enumerate(x):
        if (x_i >= s.shape[i]) or (x_i < 0):
            return True
    return False


@njit
def at_open_edge(s, x, open_bounds):
    """
    Checks whether site x is right at an edge of the sandbox.
    Only consider edges exhibiting open boundary conditions in the query,
    such that 'closed edges' are hidden from open boundary handling.

    :param s: np.array
    :param x: int position of grain drop-off
    :param open_bounds: tuple (of 2 times sandbox's dimension) of booleans specifying
                        open[True]/closed[False] boundary conditions for respective edges
    """

    # Cannot be at the edge AND off boundary
    if off_boundary(s, x):
        return False

    for i, x_i in enumerate(x):
        if (open_bounds[2*i] == True) and (x_i == 0):                   # x_i at i'th open lower edge?
            return True
        if (open_bounds[2*i+1] == True) and (x_i == s.shape[i] - 1):    # x_i at i'th open upper edge?
            return True
    return False


def get_neighbours(x):
    """
    Finds all nearest neighbours of x and returns them.

    :param x: int position of grain drop-off
    :return: array of coordinates of neighbours
    """

    nn = []
    x = list(x)

    for i, x_i in enumerate(x):
        for shift in (-1, 1):
            nn.append(tuple(x[0:i] + [x_i + shift] + x[i+1:]))

    return np.array(nn)

def get_neighbouringSlopes(s, x, neighbours):
    """
    Returns slopes (pile height differences) to all given neighbours of x with respect to x.
    Each slope value corresponds to a column vector in the neighbours-array (see also get_neighbours(x))

    :param s: np.array sandbox
    :param x: int position of grain drop-off
    :param neighbours: array of coordinates of neighbours
    :return: array of slopes
    """

    # Number of neighbours
    num = neighbours.shape[0]

    # Set all slopes to 0 initially (closed boundary conditions)
    retSlope = np.zeros(shape=num, dtype=np.int32)

    # Determine slopes to each neighbour from pile height differences
    # (if neighbour is off-boundary, just don't overwrite boundary conditions from above)
    for i in range(num):
        if not off_boundary(s, neighbours[i]):
            retSlope[i] = (s[tuple(x)] - s[tuple(neighbours[i])])

    return retSlope


def do_relaxation(s, x_array, critical_slope, open_bounds, neighbour_LUD, result, avalanche, plot_simulation=False):
    """
    Performs the avalanche relaxation mechanism recursively until all slopes are non-critical anymore.

    :param s: np.array sandbox
    :param x_array: int position of grain drop-off or an array of positions for multiple simultaneous relaxations
    :param critical_slope: int critical height of pile of sand grains
    :param open_bounds: boolean array of boundary conditions (open/closed)
    :param avalanche_stats: dict used for gathering avalanche statistics
    :param recLevel: int avalanche's recursion depth
    :return: np.array changed sandbox after relaxation process
    """

    # Dimension of the sandbox
    dim = s.ndim

    # Initialize avalanche's time duration
    result["duration"] = -1


    while True:

        # Reshape x_array if it is only a single position, such that the loop below can be used in all cases
        if x_array.ndim == 1:
            x_array = x_array.reshape((1,x_array.shape[0]))


        # Debugging
        ##if result["duration"] > 100:
        ##    print("-- Level: "+str(result["duration"])+" --| -- relaxSites: "+str(x_array.shape[0]))


        # To emulate simultaneous relaxations, do them successively for each member of x_array
        # (each position) using the same sandbox s=const for slope determination.
        # The simultaneous relaxations are meanwhile accumulated in sandbox sPrime
        sPrime = np.copy(s)

        # Note at which positions/iterations relaxation events happen
        relaxEvents = np.array([], dtype=np.uint32)

        # Loop through positions in x_array
        for it in range(x_array.shape[0]):
            x = x_array[it]

            # Dont try to relax if x is off-boundary
            if off_boundary(s, x):
                continue

            # If x is right at an 'open edge', just drop excess grains from sandpile for too large s[x]
            if s[tuple(x)] >= critical_slope and at_open_edge(s, x, open_bounds):
                sPrime[tuple(x)] = 0
                avalanche[tuple(x)] = 1
                relaxEvents = np.append(arr=relaxEvents, values=[it], axis=0)   # Bookkeeping (see below)
                continue

            ###-- Choose random nearest neighbour with maximum (and critical) slope. --###
            ###-- If no slope is critical, do nothing further.                       --###

            # Get all nearest neighbours of current site x; either from look-up or function call
            if tuple(x) in neighbour_LUD:
                neighbours = neighbour_LUD[tuple(x)]
            else:
                neighbours = get_neighbours(x)
                neighbour_LUD[tuple(x)] = neighbours   # Write to look-up dict

            # Determine all slopes between x and neighbours
            slopes = get_neighbouringSlopes(s, x, neighbours)

            # Find neightbours with at least critical slope and list their corresponding slopes
            crit_slopes_idx = np.where(slopes >= critical_slope)[0]
            crit_slopes = slopes[crit_slopes_idx]
            crit_neighbours = neighbours[crit_slopes_idx]

            # Continue loop if no slope is critical at position x
            if len(crit_slopes_idx) == 0:
                continue

            # Bookkeeping: actual relaxation event will happen at this recursion level
            relaxEvents = np.append(arr=relaxEvents, values=[it], axis=0)

            # Find neighbours with maximum slope
            current_max_slope = np.max(crit_slopes)

            ##-- Collapse excess grain stack (excess with respect to neighbour with least --##
            ##-- grains) and redistribute to all critical neighbours, i.e. drop at max    --##
            ##-- current_max_slope grains as long as there are still critical neighbours. --##

            N = len(crit_neighbours)
            max_to_drop = current_max_slope

            offset = np.random.randint(N)   # Randomly select initial drop neighbour
            for i in range(max_to_drop):
                tIdx = (i+offset) % N
                drop_site = tuple(crit_neighbours[tIdx])

                if (sPrime[tuple(x)] - sPrime[drop_site]) <= 0:
                    continue

                sPrime[drop_site] += 1
                sPrime[tuple(x)] -= 1

                # Record drop as part of the avalanche
                avalanche[drop_site] = 1

            # Point x took part in the avalanche, too; set to 1
            avalanche[tuple(x)] = 1



        ###-- STATISTICS --###
        # Increase avalanche's time duration about 1
        result["duration"] += 1

        # Increase avalanche size about the number of additional relaxation events
        result["size"] += len(relaxEvents)
        ###----------------###


        # If no relaxation actually happened the avalanche stops at this recursion level
        if len(relaxEvents) == 0:
            return s

        # Fast plotting
        if plot_simulation:
            plot_simulation.setData(sPrime)
            pg.QtGui.QApplication.processEvents()


        # Now after simultaneous relaxations at positions in x_array
        # relax all neighbours of actually relaxed positions in x_array simultaneously

        # Get all nearest neighbours of site x_array[it]; either from look-up or function call
        if tuple(x_array[relaxEvents[0]]) in neighbour_LUD:
            x_array_neighbours = neighbour_LUD[tuple(x_array[relaxEvents[0]])]
        else:
            x_array_neighbours = get_neighbours(x_array[relaxEvents[0]])
            neighbour_LUD[tuple(x_array[relaxEvents[0]])] = x_array_neighbours  # Write to look-up dict

        for it in relaxEvents[1:]:  #Skip first event as this is initial content of x_array_neighbours

            # Get all nearest neighbours of site x_array[it]; either from look-up or function call
            if tuple(x_array[it]) in neighbour_LUD:
                tmp_neighbours = neighbour_LUD[tuple(x_array[it])]
            else:
                tmp_neighbours = get_neighbours(x_array[it])
                neighbour_LUD[tuple(x_array[it])] = tmp_neighbours  # Write to look-up dict

            for row in tmp_neighbours:
                if not any(np.equal(x_array_neighbours,row).all(axis=1)):    # if not in x_array_neighbours, append
                    #if row.tolist() not in x_array_neighbours.tolist():
                    x_array_neighbours = np.append(arr=x_array_neighbours, values=[row], axis=0)

        # Use sPrime as updated sandbox for next relaxation step
        s = np.copy(sPrime)
        x_array = np.copy(x_array_neighbours)


def drive_simulation(s, site=None, amount=1):
    """
    Add a single grain of sand at site. If site is None, add randomly.
    
    :param s: np.array of sandbox
    :param site: tuple of site or None
    :param amount: int of sand grains which are added to site
    """
    
    # Make random site
    if site is None:
        site = np.random.randint(low=0, high=np.amin(s.shape), size=s.ndim)

    # Add to site
    s[tuple(site)] += 1
        

def do_simulation(s, critical_slope, total_drops, site, result_array, open_bounds, plot_simulation=False, avalanche=None):
    """
    Does entire simulation of sandbox evolution by dropping total_drops of grains on site in s.
    Drops one grain of sand at a time on site. Checks after each drop whether some sites are at
    or above critical_slope. As long as that's the case, relaxes these sites simultaniously. Then moves
    on to next drop.
    
    :param s: np.array of sandbox
    :param critical_slope: int of critical pile height
    :param total_drops: int of total amount of grains that are dropped (iterations)
    :param site: tuple of site on which the sand is dropped or None; if None, add randomly
    :param result_array: np.array in which the results of all iterations are stored
    :param plot_simulation: False or pg.ImageItem; If ImageItem, the entire evolution of the sandbox will be plot
    :param avalanche: np.array of np.arrays of bools like s or None; if None, dont store avalanche configurations,
           else store which sites took part in avalanche for each iteration
    """

    
    # Make look-up dict of points as keys and their neighbours as values; speed-up by a factor of 10
    neighbour_LUD = {}
    
    # Make temporary array to store avalanche configuration in order to calculate linear size and area
    tmp_avalanche = np.zeros_like(s, dtype=np.bool)
    
    # Flag indicating whether or not to calculate the lin_size; large avalanches (in sandboxes > 2 dimensions, > 50 length)
    # cause several 10 GB RAM consumption when calculating lin_size
    lin_size_flag = True if np.power(s.shape[0], s.ndim) > 50**3 else False
    
    #Feedback
    if lin_size_flag:
        logging.info('No calculation of "lin_size" for %s sandbox. Too large.' % str(s.shape))
    
    # Timing estimate
    estimate_time = time.time()

    # Drop sand iteratively
    for drop in xrange(total_drops):

        ## Make random site
        if site is None:
            p = np.random.randint(low=0, high=np.amin(s.shape), size=s.ndim)
        else:
            p = site
            
        # Feedback
        if drop % (5e-3 * total_drops) == 0 and drop > 0:
            # Timing estimate
            avg_time = (time.time() - estimate_time) / drop  # Average time taken for the last 0.5% of total_drops
            est_hours = avg_time * (total_drops-drop)/60**2
            est_mins = (est_hours % 1) * 60
            est_secs = (est_mins % 1) * 60
            msg = 'At drop %i of %i total drops (%.1f %s). Estimated time left: %i h %i m %i s' % (drop, total_drops, 100 * float(drop)/total_drops, "%", int(est_hours),
                  int(est_mins), int(est_secs))
            logging.info(msg)
        
        # Extract result array for current iteration
        current_result = result_array[drop]
        
        # Extract array for recording avalanches if avalanche array is given; else only store current configuration and reset after
        current_avalanche = tmp_avalanche if avalanche is None else avalanche[drop]
        
        # Add one grain of sand
        drive_simulation(s, p)

        # Relax the sandbox simultaneously
        s = do_relaxation(s=s, x_array=p, critical_slope=critical_slope, open_bounds=open_bounds, neighbour_LUD=neighbour_LUD, result=current_result,
                          avalanche=current_avalanche, plot_simulation=plot_simulation)

        
        ### RESULTS ###        
                
        # Get number of sites participating in current avalanche
        current_result['area'] = np.count_nonzero(current_avalanche)
        
        # Get the linear size of the current avalanche if there were relaxations
        relaxations = current_result['duration']
        if relaxations != 0 and len(np.where(current_avalanche)[0]) > 1 and not lin_size_flag:

            # Get coordinates of avalanche sites
            coords = np.column_stack(np.where(current_avalanche))

            # Get maximum distance between them
            try:
                current_result['lin_size'] = np.amax(pdist(coords))  # This gets slow (> 10 ms) for large (> 50 x 50) sandboxes but is still fastest choice
            
            # Memory error for large amount of avalanche sites
            except MemoryError:
                logging.warning('Memory error due to large avalanche for drop %i. No "lin_size" calculated.' % drop)
                pass  # Do nothing
                
        # Reset avalanche configuration for use in next iteration
        tmp_avalanche.fill(0)
        
        # Fast plotting
        if plot_simulation:
            plot_simulation.setData(s)
            pg.QtGui.QApplication.processEvents()

def get_criticality(sandbox, critical_slope):
    """
    Returns criticality parameter based on slope distribution
    that is 0 for a flat pile and ~1 for a critical pile.

    :param sandbox: np.array sandbox
    :param sandbox: int critical slope
    :return: float criticality parameter
    """

    slopeSum = 0

    # Loop through all axes to sum up slopes in all directions
    for i in xrange(sandbox.ndim):

        # Shift i-th axis about 1
        sShift = np.roll(sandbox, 1, axis=i)
        # Shift i-th axis about -1
        sShift2 = np.roll(sandbox, -1, axis=i)

        # Sum absolute slope values at each site (except at the 'lower' edge (no periodic boundaries!))
        it = np.nditer(sShift, flags=['multi_index'])
        while not it.finished:
            if it.multi_index[i] == 0:
                it.iternext()
                continue
            slopeSum += abs((sShift - sandbox)[it.multi_index])
            slopeSum += abs((sShift2 - sandbox)[it.multi_index])
            it.iternext()

    critParm = float(slopeSum) / sandbox.size / (critical_slope - 1)

    return critParm


def make_critical(s, critical_slope, open_bounds, max_iterations=1000000, saturation_parameter=1e-5):
    """
    Fills s with sand until a 'level' fraction of sites of the sandbox are critical.
    
    :param s: np.array of sandbox
    :param critical_slope: int critical height of sand piles
    :param level: float percentage of lattice sites which must be critical
    """

    # Make structured np.array to store results in
    result_array = np.array(np.zeros(shape=1),
                            dtype=[('duration', 'i4'), ('area', 'i4'),
                                   ('size', 'i4'), ('lin_size', 'f4')])
    # Extract result array for current iteration
    current_result = result_array[0]
    # Make look-up dict of points as keys and their neighbours as values; speed-up by a factor of 10
    neighbour_LUD = {}
    # Make temporary array to store avalanche configuration in order to calculate linear size and area
    tmp_avalanche = np.zeros_like(s, dtype=np.bool)


    # Create random critical sandpile
    critEvolution = {}
    N = 2000
    for i in xrange(max_iterations):    # Loop until sandpile is critical or
                                        # maximum number of iterations reached
        
        # Check criticality every N-th iteration
        if i % N == 0:
            critEvolution[i] = get_criticality(sandbox=s, critical_slope=critical_slope)

            # Stop loop when criticality parameter saturates
            if i > 0:
                tDiff = abs((critEvolution[i] - critEvolution[i-N])) / N
                print("i="+str(i)+", criticality parameter="+str(critEvolution[i])+", delta(crit. parm.)[i,i-N]="+str(tDiff))
                if tDiff < saturation_parameter:
                    print("Done. Sandpile critical at i=" + str(i))
                    break

        # Make random site
        site = np.random.randint(low=0, high=np.amin(s.shape), size=s.ndim)

        # Add sand at random position
        drive_simulation(s, site)

        # Relax the sandbox simultaneously
        s = do_relaxation(s=s, x_array=site, critical_slope=critical_slope, open_bounds=open_bounds,
                             neighbour_LUD=neighbour_LUD, result=current_result, avalanche=tmp_avalanche)

    return s


def get_critical_sandbox(length, dimension, model, critical_slope, open_bounds, force_new=False, path=None):
    """
    Method to load a previously saved sandbox in state of SOC and return it.
    If none is found, init a sandbox and drive it to state of SOC.
    
    :param length: int sandbox length
    :param dimension: int sandbox dimension
    :param path: str of custom location where sandbox files are or None; if None, look in default path
    :param model: str either 'btw' or 'custom'
    """

    # If we do not want to force a new critical sandbox
    if not force_new:
        
        # Prefix of default saving pattern which file needs to have
        required_prefix = 'x'.join([str(dim) for dim in [length] * dimension])
        
        # Suffix of default saving pattern which file needs to have
        required_suffix = model
        
        # Path where sandboxes are stored or given path were sandboxes are
        sandbox_path = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '../simulations/sandboxes')) if path is None else path

        # If path does exist, loop through .npy files, if patterns match, load and return
        if os.path.exists(sandbox_path):
            for sandbox in os.listdir(sandbox_path):
               
                if sandbox.endswith(".npy"):
                    
                    # Get rid of extension
                    tmp = sandbox.split('.')[0]
                    
                    # Get prefix
                    prefix = tmp.split('_')[0]
                    
                    # For several saved simulations of the same sandbox dimension, suffix can be either integer or model
                    try:
                        _ = int(tmp.split('_')[-1])
                        suffix = tmp.split('_')[-2]
                    except ValueError:
                        suffix =  tmp.split('_')[-1]
                    
                    # Take first sandbox that matches and return
                    if prefix == required_prefix and suffix == required_suffix :
                        logging.info('Loading critical %s %s model sandbox from %s in %s' % (required_prefix, required_suffix.upper(), sandbox, sandbox_path))
                        return np.load(os.path.join(sandbox_path, sandbox))
    
    # No sandboxes were found or path does not exist; init sandbox and return
    logging.info('Initializing %s sandbox.' % str((length, ) * dimension))

    # If there are open bondaries, significantly increase speed of make_critical(...) by starting from filled sandbox
    init_state=None
    if any(open_bounds):
        init_state='fill'

    s = init_sandbox(length, dimension, state=init_state, critical_slope=critical_slope)
    s = make_critical(s, critical_slope, open_bounds, max_iterations=400000, saturation_parameter=1e-6)
    return s


### Main ###


def main(length=None, dimension=None, critical_slope=None, total_drives=None, site=None, save_sim=False, save_sbox=False, plot_sim=True, plot_res=False):
    
    ### Initialization simulation variables ###
    
    # Variable describing this simulations model
    _MODEL = 'custom'
    
    # Length of sandbox; can be iterable for several simulations
    _LEN = 20 if length is None else length
    
    # Dimensions; can be iterable for several simulations
    _DIM = 2 if dimension is None else dimension
    
    # Set critical sand pile slope
    _CRIT_S = 5 if critical_slope is None else critical_slope
    
    # Number of total sand drops; in this model a drive actually adds sand so keep _SAND_DROPS
    _SAND_DROPS = 10000 if total_drives is None else total_drives
    
    # Site to drop to in sandbox; if None, drop randomly
    _SITE = site if site == None else np.array(site)

    # Whether to plot results
    _PLOT_RES = plot_res
    
    # Whether to plot the evolution of the sandbox
    _PLOT_SIM = plot_sim
    
    # Save results of simulation after simulation is done
    _SAVE_SIMULATION = save_sim
    
    # Save sandbox after simulation is done
    _SAVE_SANDBOX = save_sbox
    
    # Check for multiple lengths and dimensions
    _LEN = _LEN if isinstance(_LEN, Iterable) else [_LEN]
    _DIM = _DIM if isinstance(_DIM, Iterable) else [_DIM]
    _CRIT_S = _CRIT_S if isinstance(_CRIT_S, Iterable) else [_CRIT_S] * len(_DIM)
    
    # Do simulation for multiple sandbox lengths and dimensions in loops
    for i, _D in enumerate(_DIM):    
        for _L in _LEN:

            # Define boundary conditions
            #open_boundaries=(True,)*2*_D    # Open boundary conditions at all lower/upper edges
            open_boundaries=(False,)*2*_D  # Closed boundary conditions at all lower/upper edges
            if _D == 2:
                #open_boundaries=(False,False,False,True)    # 2-dim model, one open boundary
                #open_boundaries=(True,False,True,True)      # 2-dim model, one closed boundary
                open_boundaries=(False,True,True,False)     # 2-dim model, two closed boundaries
                pass
            if _D == 3:
                open_boundaries=(False,True,True,False,False,True)  # 3-dim model, three closed boundaries
                pass


            # Get critical sandbox
            s = get_critical_sandbox(length=_L, dimension=_D, model=_MODEL, critical_slope=_CRIT_S[i], force_new=False, open_bounds=open_boundaries)

            # Make structured np.array to store results in
            result_array = np.array(np.zeros(shape=_SAND_DROPS),
                                    dtype=[('duration', 'i4'), ('area', 'i4'),
                                           ('size', 'i4'), ('lin_size', 'f4')])
                                           
            # Array to record all avalanches; this array gets really large: 10 GB for 1e6 drops on a 100 x 100 sandbox; use only if enough RAM available
            # avalanche = np.zeros(shape=(_SAND_DROPS,) + s.shape, dtype=np.bool)
            
            # Capture start time of main loop
            start = time.time()
            
            ### Do actual simulation ###
            
            # Show simulation for 1 or 2 dims via pyqtgraph if pg_flag is False
            if _PLOT_SIM and s.ndim in (1, 2) and not pg_flag:
                app = pg.QtGui.QApplication([])
                pg.setConfigOptions(antialias=True)
                pg.setConfigOption('background', 'w')
                pg.setConfigOption('foreground', 'k')
                title = '%i Drops On %s Sandbox' % (_SAND_DROPS, ' x '.join([str(dim) for dim in s.shape]))
                sim_plotter = SimulationPlotter(s, _MODEL, title=title)
                do_simulation(s, _CRIT_S[i], _SAND_DROPS, _SITE, result_array, open_boundaries, plot_simulation=sim_plotter, avalanche=None)
                app.deleteLater()  # Important for several simulations
            # Just do simulation
            else:
                do_simulation(s, _CRIT_S[i], _SAND_DROPS, _SITE, result_array, open_boundaries, avalanche=None)

            
            # Capture time of simulation
            _RUNTIME = time.time() - start
            
            # Remove events without avalanches
            # avalanche = avalanche[~(avalanche == 0).all(axis=tuple(range(1, avalanche.ndim)))]
            
            logging.info('Needed %.2f seconds for %i dropped grains in %s sandbox' % (_RUNTIME, _SAND_DROPS, str(s.shape)))
            
            # Save the simulation results
            if _SAVE_SIMULATION:
                
                out_file = _SAVE_SIMULATION if isinstance(_SAVE_SIMULATION, str) else None
                
                save_simulation(s, result_array, _MODEL, total_drives=_SAND_DROPS, critical_slope=_CRIT_S[i], site=_SITE, out_file=out_file)
                
            # Save the resulting sandbox
            if _SAVE_SANDBOX:
                
                out_file = _SAVE_SANDBOX if isinstance(_SAVE_SANDBOX, str) else None
                
                save_sandbox(s, _MODEL, total_drives=_SAND_DROPS, critical_slope=_CRIT_S[i], site=_SITE, out_file=out_file)
                
            # Plot all results if no live plotting; pyqt and matplotlib don't work together
            if _PLOT_RES and (not _PLOT_SIM or s.ndim > 2 or pg_flag):
                
                if s.ndim == 2:
                    plot_sandbox(s, _SAND_DROPS, site=_SITE)
                
                # Plot all histograms
                for field in result_array.dtype.names:
                    plot_hist(result_array[field], field, binning=False, title=None)

            # Write timing with info to log
            with open('timing.log', 'a') as f:
                t = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime())
                msg = '%s, %i drops, %f seconds\n' % (str(s.shape), _SAND_DROPS, _RUNTIME)
                t += ':\t'
                f.write(t)
                f.write(msg)


if __name__ == "__main__":
    
    # Possibility to get simulation setup from yaml file
    parser = argparse.ArgumentParser()
    parser.add_argument('-s', '--setup', help='Yaml-file with simulation setup', required=False)
    args = vars(parser.parse_args())
    
    # Load simulation setup from yaml file
    if args['setup'] and not yaml_flag:
        with open(args['setup'], 'r') as setup:
            simulation_setup = yaml.safe_load(setup)
        logging.info('Starting simulation from setup file %s:%s' % (str(args['setup']), '\n\n\t' + '\n\t'.join(str(key)
                     + ': ' + str(simulation_setup[key]) for key in simulation_setup.keys()) + '\n'))
    # Use default values
    else:
        simulation_setup = {}
    
    # Start simulation
    main(**simulation_setup)
