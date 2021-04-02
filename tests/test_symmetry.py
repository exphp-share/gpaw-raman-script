
from collections import namedtuple
from script import symmetry, test_utils, interop
import script
import numpy as np
import pytest


Symmetry = namedtuple('Symmetry', ['oper_cart_rots', 'oper_perms', 'quotient_perms'])
DispData = namedtuple('DispData', ['atom', 'cart', 'data'])


def compose_perm(a, b): return b[a]
def compose_rot(a, b): return a @ b
def make_rot_hashable(a): return tuple(map(tuple, a.tolist()))
def make_perm_hashable(a): return tuple(a.tolist())


def grouptree_rotation_xyz():
    """ SemigroupTree of rotation matrices generated by the matrix that permutes the vector (x,y,z) into (y,z,x). """
    return test_utils.SemigroupTree(
        [np.array([[0,0,1],[1,0,0],[0,1,0]])],
        compose_rot, make_rot_hashable,
    )


# Spacegroup: C3 that permutes the cartesian axes as x->y->z->x
#  Structure: a single atom
def sym_xyz_1_atom():
    grouptree = grouptree_rotation_xyz()
    oper_cart_rots = grouptree.members
    oper_perms = np.array([[0]] * len(oper_cart_rots))
    quotient_perms = None
    return Symmetry(oper_cart_rots=oper_cart_rots, oper_perms=oper_perms, quotient_perms=quotient_perms)


# Spacegroup: C3 that permutes the cartesian axes as x->y->z->x
#  Structure: atoms at (1, 0, 0), (0, 1, 0), (0, 0, 1)
def sym_xyz_3_atom():
    grouptree = grouptree_rotation_xyz()
    oper_cart_rots = grouptree.members

    generators = [np.array([2, 0, 1])]  # generator that corresponds to the grouptree's generator
    oper_perms = grouptree.compute_homomorphism(
        get_generator = lambda gen_i, _rot: generators[gen_i],
        compose = compose_perm,
    )
    quotient_perms = None
    return Symmetry(oper_cart_rots=oper_cart_rots, oper_perms=oper_perms, quotient_perms=quotient_perms)


# Spacegroup: 1
#  Structure: atoms 0, 2, and 4 are related by translation.  atoms 1, 3, and 5 are related by translation.
def sym_quotient_translational_010101():
    quotient_perms = test_utils.SemigroupTree(
        [np.array([2,3,4,5,0,1])],
        compose_perm, make_perm_hashable,
    ).members
    oper_cart_rots = [np.eye(3)]
    oper_perms = [np.arange(len(quotient_perms[0]))]  # identity
    return Symmetry(oper_cart_rots=oper_cart_rots, oper_perms=oper_perms, quotient_perms=quotient_perms)


# Like sym_quotient_translational, but the translational symmetry is embedded in the spacegroup instead
def sym_embedded_translational_010101():
    _oper_cart_rots, oper_perms, quotient_perms = sym_quotient_translational_010101()

    # since there's no rotations, just move the quotient perms into the oper perms to embed the translational symmetry
    oper_cart_rots = [np.eye(3)] * len(quotient_perms)
    oper_perms = quotient_perms
    quotient_perms = None
    return Symmetry(oper_cart_rots=oper_cart_rots, oper_perms=oper_perms, quotient_perms=quotient_perms)


# Spacegroup: C3 that permutes the cartesian axes as x->y->z->x
#  Structure: atoms 0, 1 are related by translation, but all rotations map them to themselves
def sym_quotient_translational_00_xyz():
    oper_cart_rots = grouptree_rotation_xyz().members
    quotient_perms = np.array([[0, 1, 2], [1, 2, 0], [2, 0, 1]])
    oper_perms = [np.arange(len(quotient_perms[0]))] * len(oper_cart_rots)
    return Symmetry(oper_cart_rots=oper_cart_rots, oper_perms=oper_perms, quotient_perms=quotient_perms)


# * Tests a GeneralArrayCallbacks 'cart' axis
def test_general_array_vec():
    sym = sym_xyz_1_atom()
    callbacks = symmetry.GeneralArrayCallbacks(['cart'])
    disp_atoms, disp_carts, disp_values = zip(*[
        DispData(0, [ 0.1, 0, 0], data=np.array([ 1., 0, 0])),
        DispData(0, [-0.1, 0, 0], data=np.array([-1., 0, 0])),
    ])
    derivs = symmetry.expand_derivs_by_symmetry(
            callbacks=callbacks, disp_atoms=disp_atoms, disp_carts=disp_carts, disp_values=disp_values,
            oper_cart_rots=sym.oper_cart_rots, oper_perms=sym.oper_perms, quotient_perms=sym.quotient_perms,
    )
    derivs = np.array(derivs.tolist())
    assert np.allclose(derivs, np.array([[
        [10, 0, 0],
        [0, 10, 0],
        [0, 0, 10],
    ]]))


# * Tests rotation of outer axis of gradient when applying symmetry.
# * Tests a GeneralArrayCallbacks 'atom' axis.
def test_general_array_atom():
    sym = sym_xyz_3_atom()
    callbacks = symmetry.GeneralArrayCallbacks(['atom'])
    disp_atoms, disp_carts, disp_values = zip(*[
        # because the operator is no longer in the site symmetry, we need more disps
        DispData(0, [ 0.1,    0,    0], data=np.array([ 2.,  3.,  4.])),
        DispData(0, [-0.1,    0,    0], data=np.array([-2., -3., -4.])),
        DispData(0, [   0,  0.1,    0], data=np.array([ 5.,  6.,  7.])),
        DispData(0, [   0, -0.1,    0], data=np.array([-5., -6., -7.])),
        DispData(0, [   0,    0,  0.1], data=np.array([ 8.,  9.,  10.])),
        DispData(0, [   0,    0, -0.1], data=np.array([-8., -9., -10.])),
    ])
    print(sym.oper_perms)
    derivs = symmetry.expand_derivs_by_symmetry(
            callbacks=callbacks, disp_atoms=disp_atoms, disp_carts=disp_carts, disp_values=disp_values,
            oper_cart_rots=sym.oper_cart_rots, oper_perms=sym.oper_perms, quotient_perms=sym.quotient_perms,
    )
    derivs = np.array(derivs.tolist())
    assert np.allclose(derivs, np.array([[
        [20, 30,  40],  # deriv w.r.t. atom 0 x
        [50, 60,  70],  # deriv w.r.t. atom 0 y
        [80, 90, 100],  # deriv w.r.t. atom 0 z
    ], [
        [100, 80, 90],  # deriv w.r.t. atom 1 x
        [ 40, 20, 30],  # deriv w.r.t. atom 1 y
        [ 70, 50, 60],  # deriv w.r.t. atom 1 z
    ], [
        [60,  70, 50],  # deriv w.r.t. atom 2 x
        [90, 100, 80],  # deriv w.r.t. atom 2 y
        [30,  40, 20],  # deriv w.r.t. atom 2 z
    ]]))


def test_pure_translation():
    for (description, sym) in [
            ('SEPARATE TRANSLATIONS', sym_quotient_translational_010101()),
            ('EMBEDDED TRANSLATIONS', sym_embedded_translational_010101()),
    ]:
        print(f'TRYING {description}')
        callbacks = symmetry.GeneralArrayCallbacks([])
        disp_atoms, disp_carts, disp_values = zip(*[
            DispData(0, [ 0.1,    0,    0], data=np.array( 1.)),
            DispData(0, [-0.1,    0,    0], data=np.array(-1.)),
            DispData(0, [   0,  0.1,    0], data=np.array( 2.)),
            DispData(0, [   0, -0.1,    0], data=np.array(-2.)),
            DispData(0, [   0,    0,  0.1], data=np.array( 3.)),
            DispData(0, [   0,    0, -0.1], data=np.array(-3.)),
            DispData(1, [ 0.1,    0,    0], data=np.array( 4.)),
            DispData(1, [-0.1,    0,    0], data=np.array(-4.)),
            DispData(1, [   0,  0.1,    0], data=np.array( 5.)),
            DispData(1, [   0, -0.1,    0], data=np.array(-5.)),
            DispData(1, [   0,    0,  0.1], data=np.array( 6.)),
            DispData(1, [   0,    0, -0.1], data=np.array(-6.)),
        ])
        derivs = symmetry.expand_derivs_by_symmetry(
                callbacks=callbacks, disp_atoms=disp_atoms, disp_carts=disp_carts, disp_values=disp_values,
                oper_cart_rots=sym.oper_cart_rots, oper_perms=sym.oper_perms, quotient_perms=sym.quotient_perms,
        )
        derivs = np.array(derivs.tolist())
        assert np.allclose(derivs, np.array([
            [10, 20, 30],  # gradient w.r.t. atom 0
            [40, 50, 60],  # gradient w.r.t. atom 1
            [10, 20, 30],  # gradient w.r.t. atom 2
            [40, 50, 60],  # gradient w.r.t. atom 3
            [10, 20, 30],  # gradient w.r.t. atom 4
            [40, 50, 60],  # gradient w.r.t. atom 5
        ]))


def test_general_array_atom_quotient():
    sym = sym_quotient_translational_00_xyz()

    callbacks = symmetry.GeneralArrayCallbacks(['atom'])
    disp_atoms, disp_carts, disp_values = zip(*[
        DispData(0, [ 0.1,    0,    0], data=np.array([ 1.,  2.,  3.])),
        DispData(0, [-0.1,    0,    0], data=np.array([-1., -2., -3.])),
    ])
    derivs = symmetry.expand_derivs_by_symmetry(
            callbacks=callbacks, disp_atoms=disp_atoms, disp_carts=disp_carts, disp_values=disp_values,
            oper_cart_rots=sym.oper_cart_rots, oper_perms=sym.oper_perms, quotient_perms=sym.quotient_perms,
    )
    derivs = np.array(derivs.tolist())
    assert np.allclose(derivs, np.array([[
        [10, 20, 30],  # derivative w.r.t. atom 0 x
        [10, 20, 30],  # derivative w.r.t. atom 0 y
        [10, 20, 30],  # derivative w.r.t. atom 0 z
    ], [
        [30, 10, 20],  # derivative w.r.t. atom 1 x
        [30, 10, 20],  # derivative w.r.t. atom 1 y
        [30, 10, 20],  # derivative w.r.t. atom 1 z
    ], [
        [20, 30, 10],  # derivative w.r.t. atom 2 x
        [20, 30, 10],  # derivative w.r.t. atom 2 y
        [20, 30, 10],  # derivative w.r.t. atom 2 z
    ]]))


def test_lexically_ordered_gridpoints():
    # Check gridpoint order.
    gridpoints = interop.lexically_ordered_integer_gridpoints((3, 5, 6))
    np.testing.assert_array_equal(gridpoints[0], (0, 0, 0))
    np.testing.assert_array_equal(gridpoints[1], (0, 0, 1))
    np.testing.assert_array_equal(gridpoints[5], (0, 0, 5))
    np.testing.assert_array_equal(gridpoints[6], (0, 1, 0))
    np.testing.assert_array_equal(gridpoints[3*5*6-1], (2, 4, 5))


def test_quotient_translation_order__ase_atom():
    from script import interop
    repeats = (3, 5, 6)
    # pick an arbitrary image of the primative cell and an arbitrary lattice vector to translate it by
    initial_point = (1, 2, 3)
    translation = (1, 1, 4)
    final_point = (2, 3, 1)  # = (initial + translation) % repeats

    # Create data with a single nonzero element
    data = np.zeros(repeats)
    data[initial_point] = 99

    # Find the index of the operator corresponding to 'translation'.
    # 
    # As specified in the 'interop' module, the operators are lexically ordered.
    quotient_gridpoints = interop.lexically_ordered_integer_gridpoints(repeats)
    quotient, = np.where(np.all(quotient_gridpoints == translation, axis=1))[0]

    # Use the perm with that index to permute the data in the grid array.
    quotient_perms = list(interop.ase_repeat_translational_symmetry_perms(1, repeats))
    transformed_data = data.ravel()[quotient_perms[quotient]].reshape(repeats)

    # Verify that the data was translated to the expected location
    assert transformed_data[final_point] == 99


def test_quotient_translation_order__gpaw_flat_G():
    from script import interop
    grid_dim = (20, 40, 12)  # numbers with a fair number of divisors
    repeats = (5, 8, 3)  # supercell dimensions. (divisors of each grid dimension)
    initial_point = (6, 10, 9)  # an arbitrarily-chosen point in the grid
    translation = (3, 6, 1)  # an arbitrary primitive lattice vector
    final_point = tuple((initial_point[i] + translation[i] * grid_dim[i] // repeats[i]) % grid_dim[i] for i in range(3))

    # Get index of perm with this translation. (according to how we specify their order)
    quotient_gridpoints = interop.lexically_ordered_integer_gridpoints(repeats)
    quotient, = np.where(np.all(quotient_gridpoints == translation, axis=1))[0]

    # Create grid-aligned data with a single nonzero element
    data = np.zeros(grid_dim)
    data[initial_point] = 99

    # Use one of the perms produced by 'interop' to translate the data in this array
    quotient_perms = list(interop.gpaw_flat_G_quotient_permutations(grid_dim, repeats))
    assert len(quotient_perms) == np.product(repeats)
    transformed_data = data.ravel()[quotient_perms[quotient]].reshape(grid_dim)

    # Verify that the data was translated to the expected location
    print(np.where(transformed_data == 99))
    assert transformed_data[final_point] == 99
