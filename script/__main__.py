#!/usr/bin/env python3

from . import symmetry
from . import interop
from . import utils
from . import leffers

import functools
import os
import sys
import copy
import json

from collections import namedtuple
from datetime import datetime
import numpy as np
from ase.parallel import parprint, paropen, world
import phonopy
import ase.build
import ase.phonons
import gpaw
from gpaw import GPAW
from gpaw.lrtddft import LrTDDFT
import pickle
import warnings

from ruamel.yaml import YAML
yaml = YAML(typ='rt')

import typing as tp
T = tp.TypeVar("T")

def main():
    import argparse

    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers()

    parser.set_defaults(func=lambda args, log: parser.error('missing test name'))

    # Old script is still here so that we can maintain it and work towards
    # factoring out commonalities with the new script.
    p = subs.add_parser('raman')
    p.set_defaults(structure='ch4')
    p.add_argument('--mos2', action='store_const', dest='structure', const='mos2')
    p.add_argument('--supercell', type=(lambda s: tuple(map(int, s))), dest='supercell', default=(1,1,1))
    p.set_defaults(func=lambda args, log: main__raman_ch4(structure=args.structure, supercell=args.supercell, log=log))
    p = subs.add_parser('ep')
    p.add_argument('INPUT', help='.gpw file for unitcell, with structure and relevant parameters')
    p.add_argument('--supercell', type=(lambda s: tuple(map(int, s))), dest='supercell', default=(1,1,1))
    p.add_argument('--params-fd', help='json file with GPAW params to modify for finite displacement (supercell)')
    p.add_argument('--symmetry-tol', type=float, default=1e-5)
    p.add_argument('--disp-split', metavar="IDX,MOD", type=parse_disp_split, default=None, help='Only compute displacements with index IDX modulo MOD.  If provided, this process will stop after displacements.')
    p.add_argument('--laser-broadening', type=float, default=0.2, help='broadening in eV (imaginary part added to light freqencies)')
    p.add_argument('--phonon-broadening', type=float, default=3, help='phonon gaussian variance in cm-1')
    p.add_argument('--polarizations', type=lambda s: list(s.split(',')), default=[i+o for i in 'xyz' for o in 'xyz'], help='comma-separated list of raman polarizations to do (e.g. xx,xy,xz)')
    p.add_argument('--no-permutations', dest='do_permutations', action='store_false', help='disable all but one of the raman terms. Can drastically improve performance of the raman computation')
    p.add_argument('--laser-freqs', type=lambda s: list(map(int, s.split(','))), default=[488,532,633], help='comma-separated list of laser wavelengths (nm)')
    p.add_argument('--shift-step', type=int, default=1, help='step for x axis of raman shift (cm-1)')
    p.set_defaults(func=lambda args, log: main__elph_phonopy(
        structure_path=args.INPUT, supercell=args.supercell, params_fd_path=args.params_fd, log=log,
        symmetry_tol=args.symmetry_tol,
        disp_split=DispSplit(0, 1) if args.disp_split is None else args.disp_split,
        stop_after_displacements=args.disp_split is not None,
        laser_broadening=args.laser_broadening,
        phonon_broadening=args.phonon_broadening,
        polarizations=args.polarizations,
        laser_freqs=args.laser_freqs,
        do_permutations=args.do_permutations,
        shift_step=args.shift_step,
    ))

    p = subs.add_parser('brute-gpw')
    p.add_argument('INPUT', help='.gpw file for unitcell, with structure and relevant parameters')
    p.add_argument('--supercell', type=(lambda s: tuple(map(int, s))), dest='supercell', default=(1,1,1))
    p.set_defaults(func=lambda args, log: main__brute_gpw(structure_path=args.INPUT, supercell=args.supercell, log=log))
    args = parser.parse_args()

    with start_log_entry('gpaw.log') as log:
        args.func(args, log)

def start_log_entry(path):
    logfile = paropen(path, 'a')
    parprint(file=logfile)
    parprint('=====================================', file=logfile)
    parprint('===', datetime.now().isoformat(), file=logfile)
    return utils.Tee(logfile, sys.stdout)

DispSplit = namedtuple('DispSplit', ['index', 'mod'])
def parse_disp_split(s):
    idx, mod = map(int, s.split(','))
    assert 0 <= idx < mod, "invalid --split-index (should satisfy 0 <= IDX < MOD)"
    return DispSplit(idx, mod)

# ==============================================================================

def main__brute_gpw(structure_path, supercell, log):
    from gpaw.elph.electronphonon import ElectronPhononCoupling
    from gpaw import GPAW

    calc = GPAW(structure_path)
    supercell_atoms = make_gpaw_supercell(calc, supercell, txt=log)

    # NOTE: confusingly, Elph wants primitive atoms, but a calc for the supercell
    elph = ElectronPhononCoupling(calc.atoms, calc=supercell_atoms.calc, supercell=supercell, calculate_forces=True)
    elph.run()
    supercell_atoms.calc.wfs.gd.comm.barrier()
    elph = ElectronPhononCoupling(calc.atoms, calc=supercell_atoms.calc, supercell=supercell)
    elph.set_lcao_calculator(supercell_atoms.calc)
    elph.calculate_supercell_matrix(dump=1)
    return


def main__elph_phonopy(
        structure_path,
        params_fd_path,
        supercell,
        log,
        symmetry_tol,
        disp_split,
        stop_after_displacements,
        laser_broadening,
        phonon_broadening,
        laser_freqs,
        polarizations,
        do_permutations,
        shift_step):
    from gpaw.elph.electronphonon import ElectronPhononCoupling
    from gpaw import GPAW

    calc = GPAW(structure_path)
    if calc.wfs.kpt_u[0].C_nM is None:
        parprint(f"'{structure_path}': no wavefunctions! You must save your .gpw file with 'mode=\"all\"'!")
        sys.exit(1)

    if calc.parameters['convergence']['bands'] == 'occupied':
        parprint(f"'{structure_path}': WARNING: only occupied bands were converged!  Please converge some conduction band states as these are an explicit part of the electron-phonon computation.")

    if params_fd_path is not None:
        params_fd = json.load(open(params_fd_path))
    else:
        params_fd = {}

    # atoms = Cluster(ase.build.molecule('CH4'))
    # atoms.minimal_box(4)
    # atoms.pbc = True

    DISPLACEMENT_DIST = 1e-2  # FIXME supply as arg to gpaw

    # FIXME: There's definitely a race condition here when using disp_split if the file doesn't already exist.
    phonon = get_minimum_displacements(cachepath='phonopy_disp.yaml',
        unitcell=ase_atoms_to_phonopy(calc.atoms),
        supercell_matrix=np.diag(supercell),
        displacement_distance=DISPLACEMENT_DIST,
        phonopy_kw=dict(
            symprec=symmetry_tol,
        ),
    )
    natoms_prim = len(calc.atoms)
    disp_phonopy_sites, disp_carts = get_phonopy_displacements(phonon)
    disp_sites = phonopy_sc_indices_to_ase_sc_indices(disp_phonopy_sites, natoms_prim, supercell)

    def do_structure(supercell_atoms, name):
        forces_path = f'phonons.{name}.pckl'
        elph_path = f'elph.{name}.pckl'
        if not os.path.exists(elph_path):
            parprint(f'== computing  elph.{name}.pckl')
            elph_data = get_elph_data(supercell_atoms)
            forces = supercell_atoms.get_forces()
            pickle.dump(forces, paropen(forces_path, 'wb'), protocol=2)
            pickle.dump(elph_data, paropen(elph_path, 'wb'), protocol=2)

    # FIXME: There's definitely a race condition here when using disp_split if the file doesn't already exist.
    if os.path.exists('supercell.eq.gpw'):
        # calling .get_atoms() (instead of just accessing .atoms) is important to make sure it doesn't read cached forces
        supercell_atoms = GPAW('supercell.eq.gpw', txt=log).get_atoms()
    else:
        supercell_atoms = make_gpaw_supercell(calc, supercell, **dict(params_fd, txt=log))
        ensure_gpaw_setups_initialized(supercell_atoms.calc, supercell_atoms)
        supercell_atoms.get_potential_energy()
        supercell_atoms.calc.write('supercell.eq.gpw', mode='all')

    if disp_split.index == 0:
        do_structure(supercell_atoms, 'eq')

    for disp_index, displaced_atoms in enumerate(iter_displaced_structures(supercell_atoms, disp_sites, disp_carts)):
        supercell_atoms.set_positions(displaced_atoms.get_positions())
        if (disp_index + 1) % disp_split.mod == disp_split.index:  # + 1 because equilibrium was zero
            do_structure(supercell_atoms, f'sym-{disp_index}')

    if stop_after_displacements:
        parprint("Stopping due to usage of --disp-split. Please resume without --disp-split once all other jobs finish.")
        return

    # Read back from file to avoid an unnecessary SCF computation later
    supercell_atoms = GPAW('supercell.eq.gpw', txt=log).get_atoms()

    disp_values = [read_elph_input(f'sym-{index}') for index in range(len(disp_sites))]

    # NOTE: phonon.symmetry includes pure translational symmetries of the supercell
    #       so we use an empty quotient group
    quotient_perms = np.array([np.arange(len(supercell_atoms))])
    super_lattice = supercell_atoms.get_cell()[...]
    super_symmetry = phonon.symmetry.get_symmetry_operations()
    oper_sfrac_rots = super_symmetry['rotations']
    oper_sfrac_trans = super_symmetry['translations']
    oper_cart_rots = np.array([super_lattice.T @ Rfrac @ np.linalg.inv(super_lattice).T for Rfrac in oper_sfrac_rots])
    oper_cart_trans = oper_sfrac_trans @ super_lattice
    oper_phonopy_coperms = phonon.symmetry.get_atomic_permutations()
    oper_phonopy_deperms = np.argsort(oper_phonopy_coperms, axis=1)

    # Convert permutations by composing the following three permutations:   into phonopy order, apply oper, back to ase order
    parprint('phonopy deperms:', oper_phonopy_deperms.shape)
    deperm_phonopy_to_ase = interop.get_deperm_from_phonopy_sc_to_ase_sc(natoms_prim, supercell)
    oper_deperms = [np.argsort(deperm_phonopy_to_ase)[deperm][deperm_phonopy_to_ase] for deperm in oper_phonopy_deperms]
    del oper_phonopy_coperms, oper_phonopy_deperms

    elphsym = symmetry.ElphGpawSymmetrySource.from_setups_and_ops(
        setups=supercell_atoms.calc.wfs.setups,
        lattice=super_lattice,
        oper_cart_rots=oper_cart_rots,
        oper_cart_trans=oper_cart_trans,
        oper_deperms=oper_deperms,
        )

    if world.rank == 0:
        full_derivatives = symmetry.expand_derivs_by_symmetry(
            disp_sites,       # disp -> atom
            disp_carts,       # disp -> 3-vec
            disp_values,      # disp -> T  (displaced value, optionally minus equilibrium value)
            elph_callbacks_2(supercell_atoms.calc.wfs, elphsym, supercell=supercell),        # how to work with T
            oper_cart_rots,   # oper -> 3x3
            oper_perms=oper_deperms,       # oper -> atom' -> atom
            quotient_perms=quotient_perms,
        )

        # NOTE: confusingly, Elph wants primitive atoms, but a calc for the supercell
        elph = ElectronPhononCoupling(calc.atoms, calc=supercell_atoms.calc, supercell=supercell)
        displaced_cell_index = elph.offset
        del elph  # that's all we needed it for

        eq_Vt, eq_dH, eq_forces = read_elph_input('eq')
        for a in range(natoms_prim):
            for c in range(3):
                delta_Vt, delta_dH, delta_forces = full_derivatives[natoms_prim * displaced_cell_index + a][c]
                for sign in [-1, +1]:
                    disp = interop.AseDisplacement(atom=a, axis=c, sign=sign)
                    disp_Vt = eq_Vt + sign * DISPLACEMENT_DIST * delta_Vt
                    disp_dH = {k: eq_dH[k] + sign * DISPLACEMENT_DIST * delta_dH[k] for k in eq_dH}
                    disp_forces = eq_forces + sign * DISPLACEMENT_DIST * delta_forces
                    pickle.dump(disp_forces, paropen(f'phonons.{disp}.pckl', 'wb'), protocol=2)
                    pickle.dump((disp_Vt, disp_dH), paropen(f'elph.{disp}.pckl', 'wb'), protocol=2)
    world.barrier()

    # function to scope variables
    def do_supercell_matrix():
        # calculate_supercell_matrix breaks if parallelized over domains so parallelize over kpt instead
        # (note: it prints messages from all processes but it DOES run faster with more processes)
        supercell_atoms = GPAW('supercell.eq.gpw', txt=log, parallel={'domain': (1,1,1), 'band': 1, 'kpt': world.size}).get_atoms()

        elph = ElectronPhononCoupling(calc.atoms, supercell=supercell, calc=supercell_atoms.calc)
        elph.set_lcao_calculator(supercell_atoms.calc)
        # to initialize bfs.M_a
        ensure_gpaw_setups_initialized(supercell_atoms.calc, supercell_atoms)
        elph.calculate_supercell_matrix(dump=1)

        world.barrier()

    if not os.path.exists(f'elph.supercell_matrix.{calc.parameters["basis"]}.pckl'):
        do_supercell_matrix()

    if not os.path.exists('gqklnn.npy'):
        leffers.get_elph_elements(calc.atoms, gpw_name=structure_path, calc_fd=supercell_atoms.calc, sc=supercell)

    from ase.units import _hplanck, _c, J

    # The dipole transition matrix elements are found
    if not os.path.isfile("dip_vknm.npy"):
        leffers.get_dipole_transitions(calc)

    parprint('Computing phonons')
    ph = ase.phonons.Phonons(atoms=calc.atoms, name="phonons", supercell=supercell)
    ph.read()
    w_ph = np.array(ph.band_structure([[0, 0, 0]])[0])

    # And the Raman spectra are calculated
    for laser_nm in laser_freqs:
        w_l = _hplanck*_c*J/(laser_nm*10**(-9))
        for polarization in polarizations:
            if len(polarization) != 2:
                raise ValueError(f'invalid polarization "{polarization}", should be two characters like "xy"')
            d_i = 'xyz'.index(polarization[0])
            d_o = 'xyz'.index(polarization[1])
            name = "{}nm-{}".format(laser_nm, polarization)
            if not os.path.isfile(f"RI_{name}.npy"):
                leffers.calculate_raman(calc=calc, w_ph=w_ph, permutations=do_permutations, w_l = w_l, ramanname = name, d_i=d_i, d_o=d_o, gamma_l=laser_broadening, phonon_sigma=phonon_broadening, shift_step=shift_step)

            #And plotted
            leffers.plot_raman(relative = True, figname = f"Raman_{name}.png", ramanname = name)


def make_gpaw_supercell(calc: GPAW, supercell: tp.Tuple[int, int, int], **new_kw):
    atoms = calc.atoms

    # Take most parameters from the unit cell.
    params = copy.deepcopy(calc.parameters)
    try: del params['txt']
    except KeyError: pass

    # This makes the real space grid points identical to the primitive cell computation.
    # (by increasing the counts by a factor of a supercell dimension)
    params['gpts'] = calc.wfs.gd.N_c * supercell
    try: del params['h']
    except KeyError: pass

    # Decrease kpt count to match density in reciprocal space.
    # FIXME: if gamma is False, the new kpoints won't match the old ones.
    #        However, it doesn't seem appropriate to warn about this because ElectronPhononCoupling itself
    #        warns about gamma calculations for some reason I do not yet understand.  - ML
    old_kpts = params['kpts']
    params['kpts'] = {'size': tuple(np.ceil(calc.wfs.kd.N_c / supercell).astype(int))}  # ceil so that 1 doesn't become 0
    if isinstance(old_kpts, dict) and 'gamma' in old_kpts:
        params['kpts']['gamma'] = old_kpts['gamma']

    # warn if kpoint density could not be preserved (unless it's just one point in an aperiodic direction)
    if any((k % c != 0) and not (k, c, pbc) == (1, 1, False) for (k, c, pbc) in zip(calc.wfs.kd.N_c, supercell, atoms.pbc)):
        warnings.warn('original kpts not divisible by supercell; density in supercell will be different')

    sc_atoms = atoms * supercell
    sc_atoms.calc = GPAW(**dict(params, **new_kw))
    return sc_atoms

def get_elph_data(atoms):
    # This here is effectively what ElectronPhononCoupling.__call__ does.
    # It returns the data that should be pickled for a single displacement.
    atoms.get_potential_energy()

    calc = atoms.calc
    if not isinstance(calc, GPAW):
        calc = calc.dft  # unwrap DFTD3 wrapper

    Vt_sG = calc.wfs.gd.collect(calc.hamiltonian.vt_sG, broadcast=True)
    dH_asp = interop.gpaw_broadcast_array_dict_to_dicts(calc.hamiltonian.dH_asp)
    return Vt_sG, dH_asp

def elph_callbacks(wfs_with_symmetry: gpaw.wavefunctions.base.WaveFunctions, supercell):
    elphsym = symmetry.ElphGpawSymmetrySource.from_wfs_with_symmetry(wfs_with_symmetry)
    return elph_callbacks_2(wfs_with_symmetry, elphsym, supercell)

# FIXME: rename (just different args)
def elph_callbacks_2(wfs: gpaw.wavefunctions.base.WaveFunctions, elphsym: symmetry.ElphGpawSymmetrySource, supercell):
    Vt_part = symmetry.GpawLcaoVTCallbacks(wfs, elphsym, supercell=supercell)
    dH_part = symmetry.GpawLcaoDHCallbacks(wfs, elphsym)
    forces_part = symmetry.GeneralArrayCallbacks(['atom', 'cart'])
    return symmetry.TupleCallbacks(Vt_part, dH_part, forces_part)

def read_elph_input(displacement: tp.Union[interop.AseDisplacement, str]) -> tp.Tuple[np.ndarray, np.ndarray, np.ndarray]:
    Vt_sG, dH_asp = pickle.load(open(f'elph.{displacement}.pckl', 'rb'))
    forces = pickle.load(open(f'phonons.{displacement}.pckl', 'rb'))
    return Vt_sG, dH_asp, forces

def ensure_gpaw_setups_initialized(calc, atoms):
    """ Initializes the Setups of a GPAW instance without running a groundstate computation. """
    calc._set_atoms(atoms)  # FIXME private method
    calc.initialize()
    calc.set_positions(atoms)  # FIXME: Apparently this breaks if there is domain parallelism? What?!?!?!

# ==============================================================================

def main__raman_ch4(structure, supercell, log):
    import ase.build

    from gpaw.lrtddft.spectrum import polarizability
    from gpaw.cluster import Cluster
    from gpaw import GPAW, FermiDirac

    #=============================================
    # Settings

    # Input structure
    relax_grid_sep = 0.22  # GPAW finite grid size
    vacuum_sep = 3.5
    pbc = False
    if structure == 'ch4':
        def get_unrelaxed_structure():
            atoms = Cluster(ase.build.molecule('CH4'))
            atoms.minimal_box(vacuum_sep, h=relax_grid_sep)
            return atoms
    elif structure == 'mos2':
        def get_unrelaxed_structure():
            atoms = Cluster(ase.build.mx2('MoS2'))
            atoms.center(vacuum=vacuum_sep, axis=2)
            return atoms

    # Calculator (general settings)
    make_calc = functools.partial(GPAW,
            occupations=FermiDirac(width=0.1),
            symmetry={'point_group': False},
            txt=log,
    )

    # Relaxation settings
    make_calc_relax = functools.partial(make_calc,
            h=relax_grid_sep,
    )

    # Args for computations on displaced structures
    raman_grid_sep = 0.25 # In the example, they use a larger spacing here than during relaxation.
                          # (TODO: but why? On CH4 I observe that this to leads to equilibrium forces of
                          #        0.067 ev/A, which seems to compromise our "energy minimum" state...)
    num_converged_bands = 10
    num_total_bands = 20
    make_calc_raman = functools.partial(make_calc,
            h=raman_grid_sep,
            convergence={
                'eigenstates': 1.e-5,
                'bands': num_converged_bands,
            },
            eigensolver='cg',
            nbands=num_total_bands,
    )
    supercell_matrix = [[supercell[0], 0, 0], [0, supercell[1], 0], [0, 0, supercell[2]]]
    displacement_distance = 1e-2

    # ----------
    # Excitation settings (for polarizability)
    ex_kw = {'restrict': {'jend':num_converged_bands-1}}
    omega = 5.0 # eV
    get_polarizability = functools.partial(polarizability, omega=omega, form='v', tensor=True)
    subtract_equilibrium_polarizability = False

    # for testing purposes
    also_try_brute_force_raman = False

    #=============================================
    # Process

    disp_filenames = {
        'ex': {'eq': 'raman-eq.ex.gz', 'disp': 'raman-{:04}.ex.gz'},
        'force': {'eq': 'force-set-eq.npy', 'disp': 'force-set-{:04}.npy'},
    }

    # Relax
    unrelaxed_atoms = get_unrelaxed_structure()
    unrelaxed_atoms.pbc = pbc
    unrelaxed_atoms.calc = make_calc_relax()
    relax_atoms(outpath='relaxed.vasp', atoms=unrelaxed_atoms)

    # Phonopy displacements
    phonon = get_minimum_displacements(cachepath='phonopy_disp.yaml',
            unitcell=phonopy.interface.calculator.read_crystal_structure('relaxed.vasp', interface_mode='vasp')[0],
            supercell_matrix=supercell_matrix,
            displacement_distance=displacement_distance,
    )

    # Computing stuff at displacements
    eq_atoms = Cluster(phonopy_atoms_to_ase(phonon.supercell))
    eq_atoms.pbc = pbc
    if raman_grid_sep != relax_grid_sep:
        eq_atoms.minimal_box(vacuum_sep, h=raman_grid_sep)
    eq_atoms.calc = make_calc_raman()

    force_sets = make_force_sets_and_excitations(cachepath='force-sets.npy',
            disp_filenames=disp_filenames, phonon=phonon,
            atoms=eq_atoms, ex_kw=ex_kw,
    )
    phonon.set_forces(force_sets)

    # Applying symmetry
    cart_pol_derivs = expand_raman_by_symmetry(cachepath='raman-cart.npy',
            phonon=phonon,
            disp_filenames=disp_filenames, get_polarizability=get_polarizability, ex_kw=ex_kw,
            subtract_equilibrium_polarizability=subtract_equilibrium_polarizability,
    )

    # Phonopy part 2
    gamma_eigendata = get_eigensolutions_at_q(cachepath='eigensolutions-gamma.npz',
            phonon=phonon, q=[0, 0, 0],
    )

    # Raman of modes
    get_mode_raman(outpath='mode-raman-gamma.npy',
            eigendata=gamma_eigendata, cart_pol_derivs=cart_pol_derivs,
    )

    if also_try_brute_force_raman:
        eq_atoms = Cluster(phonopy_atoms_to_ase(phonon.supercell))
        eq_atoms.pbc = pbc
        if raman_grid_sep != relax_grid_sep:
            eq_atoms.minimal_box(vacuum_sep, h=raman_grid_sep)
        eq_atoms.calc = make_calc_raman()

        get_mode_raman_brute_force(
            eigendata=gamma_eigendata, atoms=eq_atoms, displacement_distance=displacement_distance,
            get_polarizability=get_polarizability, ex_kw=ex_kw,
        )

# ==================================
# Steps of the procedure.  Each function caches their results, for restart purposes.

def relax_atoms(outpath, atoms):
    from ase import optimize

    if os.path.exists(outpath):
        parprint(f'Found existing {outpath}')
        return
    world.barrier()
    parprint(f'Relaxing structure... ({outpath})')

    dyn = optimize.FIRE(atoms)
    dyn.run(fmax=0.05)
    # FIXME: consider using something else to write, like pymatgen.io.vasp.Poscar with significant_figures=15.
    #        ASE always writes {:11.8f} in frac coords, which can be a dangerous amount of rounding
    #        for large unit cells.
    atoms.write(outpath, format='vasp')


# Get displacements using phonopy
def get_minimum_displacements(
        cachepath: str,
        unitcell: phonopy.structure.atoms.PhonopyAtoms,
        supercell_matrix: np.ndarray,
        displacement_distance: float,
        phonopy_kw: dict = {},
        ):
    # note: applying phonopy_kw on load is necessary because phonopy will recompute symmetry
    load = lambda: phonopy.load(cachepath, produce_fc=False, **phonopy_kw)
    if os.path.exists(cachepath):
        parprint(f'Found existing {cachepath}')
        return load()
    world.barrier()  # avoid race condition where rank 0 creates file before others enter
    parprint(f'Getting displacements... ({cachepath})')

    if world.rank == 0:
        phonon = phonopy.Phonopy(unitcell, supercell_matrix, factor=phonopy.units.VaspToTHz, **phonopy_kw)
        phonon.generate_displacements(distance=displacement_distance)
        parprint(f'Saving displacements...')
        phonon.save(cachepath)

    world.barrier()
    parprint(f'Loading displacements...')
    return load()


def make_force_sets_and_excitations(cachepath, disp_filenames, phonon, atoms, ex_kw):
    if os.path.exists(cachepath):
        parprint(f'Found existing {cachepath}')
        return np.load(cachepath)
    world.barrier()
    parprint(f'Computing force sets and polarizability data at displacements... ({cachepath})')

    eq_atoms = atoms.copy()
    def iter_displacement_files():
        eq_force_filename = disp_filenames['force']['eq']
        eq_ex_filename = disp_filenames['ex']['eq']
        yield 'eq', eq_force_filename, eq_ex_filename, eq_atoms

        disp_phonopy_sites, disp_carts = get_phonopy_displacements(phonon)
        for i, disp_atoms in enumerate(iter_displaced_structures(atoms, disp_phonopy_sites, disp_carts)):
            force_filename = disp_filenames['force']['disp'].format(i)
            ex_filename = disp_filenames['ex']['disp'].format(i)
            yield 'disp', force_filename, ex_filename, disp_atoms

    # Make files for one displacement at a time
    for disp_kind, force_filename, ex_filename, disp_atoms in iter_displacement_files():
        if os.path.exists(ex_filename):
            continue
        world.barrier()
        atoms.set_positions(disp_atoms.get_positions())

        disp_forces = atoms.get_forces()
        ex = LrTDDFT(atoms.calc, **ex_kw)
        if disp_kind == 'eq':
            # For inspecting the impact of differences in the calculator
            # between ionic relaxation and raman computation.
            parprint('Max equilibrium force during raman:', np.absolute(disp_forces).max())
        if world.rank == 0:
            np.save(force_filename, disp_forces)
        ex.write(ex_filename)

    # combine force sets into one file
    force_sets = np.array([
        np.load(disp_filenames['force']['disp'].format(i))
        for i in range(len(phonon.get_displacements()))
    ])
    np.save(cachepath, force_sets)
    for i in range(len(phonon.get_displacements())):
        os.unlink(disp_filenames['force']['disp'].format(i))
    return force_sets


def expand_raman_by_symmetry(cachepath,
                             phonon,
                             disp_filenames,
                             get_polarizability,
                             ex_kw,
                             subtract_equilibrium_polarizability):
    if os.path.exists(cachepath):
        parprint(f'Found existing {cachepath}')
        return np.load(cachepath)
    world.barrier()
    parprint(f'Expanding raman data by symmetry... ({cachepath})')

    disp_phonopy_sites, disp_carts = get_phonopy_displacements(phonon)

    prim_symmetry = phonon.primitive_symmetry.get_symmetry_operations()
    lattice = phonon.primitive.get_cell()[...]
    carts = phonon.primitive.get_positions()

    oper_frac_rots = prim_symmetry['rotations']
    oper_frac_trans = prim_symmetry['translations']
    oper_cart_rots = np.array([np.linalg.inv(lattice).T @ R @ lattice.T for R in oper_frac_rots])
    oper_cart_trans = oper_frac_trans @ lattice

    oper_deperms = []
    for cart_rot, cart_trans in zip(oper_cart_rots, oper_cart_trans):
        carts = phonon.primitive.get_positions()
        transformed_carts = carts @ cart_rot.T + cart_trans
        oper_deperms.append(get_deperm(carts, transformed_carts, lattice))
    oper_deperms = np.array(oper_deperms)

    disp_tensors = np.array([
        get_polarizability(LrTDDFT.read(disp_filenames['ex']['disp'].format(i), **ex_kw))
        for i in range(len(disp_phonopy_sites))
    ])
    if subtract_equilibrium_polarizability:
        disp_tensors -= get_polarizability(LrTDDFT.read(disp_filenames['ex']['eq'], **ex_kw))

    pol_derivs = symmetry.expand_derivs_by_symmetry(
        disp_phonopy_sites,
        disp_carts,
        disp_tensors,
        symmetry.Tensor2Callbacks(),
        oper_cart_rots,
        oper_deperms,
    )
    pol_derivs = np.array(pol_derivs.tolist())  # (n,3) dtype=object --> (n,3,3,3) dtype=complex

    np.save(cachepath, pol_derivs)
    return pol_derivs


def get_eigensolutions_at_q(cachepath, phonon, q):
    if os.path.exists('eigensolutions-gamma.npz'):
        parprint('Found existing eigensolutions-gamma.npz')
        return dict(np.load(cachepath))
    world.barrier()
    parprint('Diagonalizing dynamical matrix at gamma... (eigensolutions-gamma.npz)')

    phonon.produce_force_constants()
    frequencies, eigenvectors = phonon.get_frequencies_with_eigenvectors(q)
    out = dict(
        atom_masses=phonon.masses,
        frequencies=frequencies,
        eigenvectors=eigenvectors.T, # store as rows
    )
    np.savez(cachepath, **out)
    return out


def get_mode_raman(outpath, eigendata, cart_pol_derivs):
    if os.path.exists(outpath):
        parprint(f'Found existing {outpath}')
        return
    world.barrier()
    parprint(f'Computing mode raman tensors... ({outpath})')

    cart_pol_derivs = np.load('raman-cart.npy')
    mode_pol_derivs = []
    for row in eigendata['eigenvectors']:
        mode_displacements = eigendata['atom_masses'].repeat(3) ** -0.5 * row
        mode_displacements /= np.linalg.norm(mode_displacements)

        #  ∂α_ij          ∂α_ij  ∂x_ak
        #  -----  = sum ( -----  ----- )
        #  ∂u_n     a,k   ∂x_ak  ∂u_n
        #
        #         = dot product of (3n-dimensional gradient of ∂α_ij)
        #                     with (3n-dimensional displacement vector of mode n)
        mode_pol_deriv = np.dot(
            # move i and j (axes 2 and 3) to the outside and combine axes 0 and 1 (x components)
            cart_pol_derivs.transpose((2, 3, 0, 1)).reshape((9, -1)),
            mode_displacements,
        ).reshape((3, 3))
        mode_pol_derivs.append(mode_pol_deriv)
    np.save(outpath, mode_pol_derivs)


# For testing purposes: Compute raman by getting polarizability at +/- displacements along mode
def get_mode_raman_brute_force(eigendata, atoms, displacement_distance, get_polarizability, ex_kw):
    if os.path.exists('mode-raman-gamma-expected.npy'):
        parprint('Found existing mode-raman-gamma-expected.npy')
        return
    world.barrier()
    parprint('Computing mode raman tensors... (mode-raman-gamma-expected.npy)')

    eq_positions = atoms.get_positions().copy()

    mode_pol_derivs = []
    for i,row in enumerate(eigendata['eigenvectors']):
        mode_displacements = eigendata['atom_masses'][:, None] ** -0.5 * row.reshape(-1, 3)
        mode_displacements /= np.linalg.norm(mode_displacements)

        atoms.set_positions(eq_positions + mode_displacements * displacement_distance)
        atoms.get_forces()
        # FIXME: These seemingly redundant reads served a purpose at some point but I never documented it.
        #        Now that LrTDDFT has this "redesigned API" they might not even do anything at all? Test this.
        LrTDDFT(atoms.calc, **ex_kw).write(f'mode-raman-{i}+.ex.gz')
        pol_plus = get_polarizability(LrTDDFT.read(f'mode-raman-{i}+.ex.gz', **ex_kw))

        atoms.set_positions(eq_positions - mode_displacements * displacement_distance)
        atoms.get_forces()
        LrTDDFT(atoms.calc, **ex_kw).write(f'mode-raman-{i}-.ex.gz')
        pol_minus = get_polarizability(LrTDDFT.read(f'mode-raman-{i}-.ex.gz', **ex_kw))

        mode_pol_derivs.append((pol_plus - pol_minus)/(2*displacement_distance))

    if world.rank == 0:
        np.save('mode-raman-gamma-expected.npy', mode_pol_derivs)
    world.barrier()

#----------------

def get_deperm(
        carts_from,  # Nx3
        carts_to,  # Nx3
        lattice,  # 3x3 matrix or ASE Cell, each row is a lattice vector
        tol: float = 1e-5,
):
    from phonopy.structure.cells import compute_permutation_for_rotation

    # Compute the inverse permutation on coordinates, which is the
    # forward permutation on metadata ("deperm").
    #
    # I.e. ``fracs_translated[deperm] ~~ fracs_original``
    fracs_from = carts_from @ np.linalg.inv(lattice)
    fracs_to = carts_to @ np.linalg.inv(lattice)
    return compute_permutation_for_rotation(
        fracs_to, fracs_from, lattice[...].T, tol,
    )

# ==============================================================================

Displacements = tp.List[tp.Tuple[int, tp.List[float]]]
PhonopyScIndex = int  # index of a supercell atom, in phonopy's supercell ordering
AseScIndex = int  # index of a supercell atom, in ASE's supercell ordering

def get_phonopy_displacements(phonon: phonopy.Phonopy):
    """ Get displacements as arrays of ``phonopy_atom`` and ``[dx, dy, dz]`` (cartesian).

    ``phonopy_atom`` is the displaced atom index according to phonopy's supercell ordering convention.
    Mind that this is different from ASE's convention. """
    return tuple(map(list, zip(*[(i, xyz) for (i, *xyz) in phonon.get_displacements()])))

def phonopy_sc_indices_to_ase_sc_indices(phonopy_disp_atoms, natoms, supercell):
    """ Takes an array of atom indices in phonopy's supercell ordering convention and converts it to ASE's convention. """
    # use inverse perm to permute sparse indices
    deperm_phonopy_to_ase = interop.get_deperm_from_phonopy_sc_to_ase_sc(natoms, supercell)  # ase index -> phonopy index
    inv_deperm_phonopy_to_ase = np.argsort(deperm_phonopy_to_ase)  # phonopy index -> ase index
    return inv_deperm_phonopy_to_ase[phonopy_disp_atoms]  # ase indices

def iter_displaced_structures(atoms, disp_sites, disp_carts):
    # Don't use phonon.get_supercells_with_displacements as these may be translated
    # a bit relative to the original atoms if you used something like 'minimum_box'.
    # (resulting in absurd forces, e.g. all components positive at equilibrium)
    eq_atoms = atoms.copy()
    assert len(disp_sites) == len(disp_carts)
    for i, disp in zip(disp_sites, disp_carts):
        disp_atoms = eq_atoms.copy()
        positions = disp_atoms.get_positions()
        positions[i] += disp
        disp_atoms.set_positions(positions)
        yield disp_atoms

# ==============================================================================

def phonopy_atoms_to_ase(atoms):
    atoms = ase.Atoms(
        symbols=atoms.get_chemical_symbols(),
        positions=atoms.get_positions(),
        cell=atoms.get_cell(),
    )
    return atoms

def ase_atoms_to_phonopy(atoms):
    atoms = phonopy.structure.atoms.PhonopyAtoms(
        symbols=atoms.get_chemical_symbols(),
        positions=atoms.get_positions(),
        cell=atoms.get_cell(),
    )
    return atoms

if __name__ == '__main__':
    main()
