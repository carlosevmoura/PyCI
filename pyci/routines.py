# This file is part of PyCI.
#
# PyCI is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your
# option) any later version.
#
# PyCI is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License
# for more details.
#
# You should have received a copy of the GNU General Public License
# along with PyCI. If not, see <http://www.gnu.org/licenses/>.

r"""PyCI additional routines module."""

from functools import partial
from itertools import combinations

from typing import Any, List, Sequence, Union

import numpy as np
import scipy.sparse.linalg as sp

from . import pyci


__all__ = [
    "add_excitations",
    "add_seniorities",
    "add_gkci",
    "solve",
    "solve_cepa0",
]


def add_excitations(wfn: pyci.wavefunction, *excitations: Sequence[int], ref=None) -> None:
    r"""
    Add multiple excitation levels of determinants to a wave function.

    Convenience function.

    Parameters
    ----------
    wfn : pyci.wavefunction
        Wave function.
    excitations : Sequence[int]
        List of excitation levels of determinants to add.
    ref : np.ndarray, optional
        Reference determinant by which to determine excitation levels.
        Default is the Hartree-Fock determinant.

    """
    for e in excitations:
        wfn.add_excited_dets(e, ref=ref)


def add_seniorities(wfn: pyci.fullci_wfn, *seniorities: Sequence[int]) -> None:
    r"""
    Add determinants of the specified seniority/ies to the wave function.

    Parameters
    ----------
    wfn : pyci.fullci_wfn
        FullCI wave function.
    seniorities : Sequence[int]
        List of seniorities of determinants to add.

    """
    # Check wave function
    if not isinstance(wfn, pyci.fullci_wfn):
        raise TypeError("wfn must be a pyci.fullci_wfn")

    # Check specified seniorities
    smin = wfn.nocc_up - wfn.nocc_dn
    smax = min(wfn.nocc_up, wfn.nvir_up)
    if any(s < smin or s > smax or s % 2 != smin % 2 for s in seniorities):
        raise ValueError("invalid seniority number specified")

    # Make seniority-zero occupation vectors
    sz_wfn = pyci.doci_wfn(wfn.nbasis, wfn.nocc_up)
    sz_wfn.add_all_dets()
    occ_up_array = sz_wfn.to_occ_array()
    del sz_wfn

    # Make working arrays
    brange = np.arange(wfn.nbasis, dtype=pyci.c_int)
    occs = np.empty((2, wfn.nocc_up), dtype=pyci.c_int)

    # Add determinants of specified seniorities
    for s in seniorities:
        if not s:
            # Seniority-zero
            for occs_up in occ_up_array:
                occs[0, :] = occs_up
                occs[1, :] = occs_up
                wfn.add_occs(occs)
        else:
            # Seniority-nonzero
            pairs = (wfn.nocc - s) // 2
            if pairs == wfn.nocc_dn:
                for occs_up in occ_up_array:
                    occs[0, :] = occs_up
                    for occs_dn in combinations(occs_up, wfn.nocc_dn):
                        occs[1, : wfn.nocc_dn] = occs_dn
                        wfn.add_occs(occs)
            elif not pairs:
                for occs_up in occ_up_array:
                    occs[0, :] = occs_up
                    virs_up = np.setdiff1d(brange, occs_up, assume_unique=True)
                    for occs_dn in combinations(virs_up, wfn.nocc_dn):
                        occs[1, : wfn.nocc_dn] = occs_dn
                        wfn.add_occs(occs)
            else:
                for occs_up in occ_up_array:
                    occs[0, :] = occs_up
                    virs_up = np.setdiff1d(brange, occs_up, assume_unique=True)
                    for occs_i_dn in combinations(occs_up, pairs):
                        occs[1, :pairs] = occs_i_dn
                        for occs_a_dn in combinations(virs_up, wfn.nocc_dn - pairs):
                            occs[1, pairs : wfn.nocc_dn] = occs_a_dn
                            wfn.add_occs(occs)


def add_gkci(
    wfn: pyci.wavefunction,
    t: float = -0.5,
    p: float = 1.0,
    mode: Union[str, List[int]] = "cntsp",
    node_dim: int = None,
) -> None:
    r"""
    Add determinants to the wave function according to the odometer algorithm (Griebel-Knapeck CI).

    Adapted from Gaby and Farnaz's original code.

    Parameters
    ----------
    wfn : pyci.wavefunction
        Wave function.
    t : float, default=-0.5
        ???
    p : float, default=1.0
        ???
    mode : List[int] or ('cntsp' | 'gamma' | 'cubic'), default='cntsp'
        Node pattern.
    node_dim : int, default=None
        Number of nodes (if specified).

    """
    # Check arguments
    if isinstance(mode, str):
        if mode != "gamma" and node_dim is not None:
            raise ValueError("node_dim must not be specified with mode " + mode)
        elif mode == "gamma" and node_dim is None:
            raise ValueError("node_dim must be specified with mode 'gamma'")

        # Construct nodes
        if mode == "cntsp":
            nodes = list()
            shell = 1
            while len(nodes) < wfn.nbasis:
                nodes.extend([shell - 1.0] * shell ** 2)
                shell += 1
        elif mode == "gamma":
            raise NotImplementedError
        elif mode == "cubic":
            raise NotImplementedError
        else:
            raise ValueError("mode must be one of ('cntsp', 'gamma', 'cubic'")
    else:
        nodes = mode
    nodes = np.array(nodes[: wfn.nbasis])

    # Run odometer algorithm
    if isinstance(wfn, (pyci.doci_wfn, pyci.genci_wfn)):
        _odometer_one_spin(wfn, nodes, t, p)
    elif isinstance(wfn, pyci.fullci_wfn):
        _odometer_two_spin(wfn, nodes, t, p)
    else:
        raise TypeError("wfn must be a pyci.{doci,fullci,genci}_wfn")


def _odometer_one_spin(wfn: pyci.one_spin_wfn, nodes: List[int], t: float, p: float) -> None:
    r"""Run the odometer algorithm for a one-spin wave function."""
    aufbau_occs = np.arange(wfn.nocc_up, dtype=pyci.c_int)
    new_occs = np.copy(aufbau_occs)
    old_occs = np.copy(aufbau_occs)
    # Index of last electron
    j_electron = wfn.nocc_up - 1
    # Compute cost of the most important neglected determinant
    nodes_s = nodes[new_occs]
    qs_neg = np.sum(nodes_s[:-1]) * p + (t + 1) * nodes[-1] * p
    # Select determinants
    while True:
        if new_occs[wfn.nocc_up - 1] >= wfn.nbasis:
            # Reject determinant b/c of occupying an inactive or non-existant orbital;
            # go back to last-accepted determinant and excite the previous electron
            new_occs[:] = old_occs
            j_electron -= 1
        else:
            # Compute nodes and cost of occupied orbitals
            nodes_s = nodes[new_occs]
            qs = np.sum(nodes_s) + t * np.max(nodes_s)
            if qs < qs_neg:
                # Accept determinant and excite the last electron again
                wfn.add_occs(new_occs)
                j_electron = wfn.nocc_up - 1
            else:
                # Reject determinant because of high cost; go back to last-accepted
                # determinant and excite the previous electron
                new_occs[:] = old_occs
                j_electron -= 1
        if j_electron < 0:
            # Done
            break
        # Record last-accepted determinant and excite j_electron
        old_occs[:] = new_occs
        new_occs[j_electron] += 1
        if j_electron != wfn.nocc_up - 1:
            for k in range(j_electron + 1, wfn.nocc_up):
                new_occs[k] = new_occs[j_electron] + k - j_electron


def _odometer_two_spin(wfn: pyci.two_spin_wfn, nodes: List[int], t: float, p: float) -> None:
    r"""Run the odometer algorithm for a two-spin wave function."""
    aufbau_occs = np.arange(wfn.nocc, dtype=pyci.c_int)
    aufbau_occs[wfn.nocc_up :] -= wfn.nocc_up
    new_occs = np.copy(aufbau_occs)
    old_occs = np.copy(aufbau_occs)
    # Index of last electron
    j_electron = wfn.nocc - 1
    # Compute cost of the most important neglected determinant
    nodes_up = nodes[new_occs[: wfn.nocc_up]]
    nodes_dn = nodes[new_occs[wfn.nocc_up :]]
    q_up_neg = np.sum(nodes_up[:-1]) * p + (t + 1) * nodes[-1] * p
    q_dn_neg = np.sum(nodes_dn[:-1]) * p + (t + 1) * nodes[-1] * p
    # Select determinants
    while True:
        if max(new_occs[wfn.nocc_up - 1], new_occs[wfn.nocc - 1]) >= wfn.nbasis:
            # Reject determinant b/c of occupying an inactive or non-existant orbital;
            # go back to last-accepted determinant and excite the previous electron
            new_occs[:] = old_occs
            j_electron -= 1
        else:
            # Compute nodes and cost of occupied orbitals
            nodes_up = nodes[new_occs[: wfn.nocc_up]]
            nodes_dn = nodes[new_occs[wfn.nocc_up :]]
            q_up = np.sum(nodes_up) + t * np.max(nodes_up)
            q_dn = np.sum(nodes_dn) + t * np.max(nodes_dn)
            if q_up < q_up_neg and q_dn < q_dn_neg:
                # Accept determinant and excite the last electron again
                wfn.add_occs(new_occs.reshape(2, -1))
                j_electron = wfn.nocc - 1
            else:
                # Reject determinant because of high cost; go back to last-accepted
                # determinant and excite the previous electron
                new_occs[:] = old_occs
                j_electron -= 1
        if j_electron < 0:
            # Done
            break
        # Record last-accepted determinant and excite j_electron
        old_occs[:] = new_occs
        new_occs[j_electron] += 1
        if j_electron < wfn.nocc_up:
            # excite spin-up electron
            for k in range(j_electron + 1, wfn.nocc_up):
                new_occs[k] = new_occs[j_electron] + k - j_electron
        elif j_electron < wfn.nocc - 1:
            # excite spin-down electron
            for k in range(j_electron + 1, wfn.nocc):
                new_occs[k] = new_occs[j_electron] + k - j_electron


def solve(
    *args: Any,
    n: int = 1,
    c0: np.ndarray = None,
    ncv: int = None,
    maxiter: int = 5000,
    tol: float = 1.0e-12,
):
    r"""
    Solve a CI eigenproblem.

    Parameters
    ----------
    args : (pyci.sparse_op,) or (pyci.hamiltonian, pyci.wavefunction)
        System to solve.
    n : int, optional
        Number of lowest eigenpairs to find.
    c0 : np.ndarray, optional
        Initial guess for lowest eigenvector.
    ncv : int, optional
        Number of Lanczos vectors to use.
    maxiter : int, optional
        Maximum number of iterations to perform.
    tol : float, optional
        Convergence tolerance.

    Returns
    -------
    es : np.ndarray
        Energies.
    cs : np.ndarray
        Coefficient vectors.

    """
    # Handle inputs
    if len(args) == 1:
        op = args[0]
    elif len(args) == 2:
        op = pyci.sparse_op(*args)
    else:
        raise ValueError("must pass `ham, wfn` or `op`")
    # Handle length-1 eigenproblem
    if op.shape[1] == 1:
        return (
            np.full(1, op.get_element(0, 0) + op.ecore, dtype=pyci.c_double),
            np.ones((1, 1), dtype=pyci.c_double),
        )
    # Prepare initial guess
    if c0 is None:
        c0 = np.zeros(op.shape[1], dtype=pyci.c_double)
        c0[0] = 1
    else:
        c0 = np.concatenate((c0, np.zeros(op.shape[1] - c0.shape[0], dtype=pyci.c_double)))
    # Solve eigenproblem
    es, cs = sp.eigsh(
        sp.LinearOperator(matvec=op, shape=op.shape),
        k=n,
        v0=c0,
        ncv=ncv,
        maxiter=maxiter,
        tol=tol,
        which="SA",
    )
    # Return result
    es += op.ecore
    return es, cs.transpose()


def solve_cepa0(*args, e0=None, c0=None, refind=0, maxiter=5000, tol=1.0e-12, lstsq=False):
    r"""
    Solve a CEPA0 problem.

    Parameters
    ----------
    args : (pyci.sparse_op,) or (pyci.hamiltonian, pyci.wavefunction)
        System to solve.
    c0 : np.ndarray, optional
        Initial guess for lowest eigenvector.
    refind : int, optional
        Index of determinant to use as reference.
    maxiter : int, optional
        Maximum number of iterations to perform.
    tol : float, optional
        Convergence tolerance.
    lstsq : bool, optional
        Whether to find the least-squares solution.

    Returns
    -------
    e : float
        Energy.
    c : np.ndarray
        Coefficient vector.

    """
    # Handle inputs
    if len(args) == 1:
        op = args[0]
    elif len(args) == 2:
        op = pyci.sparse_op(*args)
    else:
        raise ValueError("must pass `ham, wfn` or `op`")
    # Prepare initial guess
    c0 = np.zeros(op.shape[1], dtype=pyci.c_double) if c0 is None else c0 / c0[refind]
    c0[refind] = op.get_element(refind, refind) if e0 is None else e0
    # Prepare left-hand side matrix
    lhs = sp.LinearOperator(
        matvec=partial(op.matvec_cepa0, refind=refind),
        rmatvec=partial(op.rmatvec_cepa0, refind=refind),
        shape=op.shape,
    )
    # Prepare right-hand side vector
    rhs = op.rhs_cepa0(refind=refind)
    rhs -= op.matvec_cepa0(c0, refind=refind)
    # Solve equations
    if lstsq:
        result = sp.lsqr(lhs, rhs, iter_lime=maxiter, btol=tol, atol=tol)
    else:
        result = sp.lgmres(lhs, rhs, maxiter=maxiter, tol=tol, atol=tol)
    # Return result
    c = result[0]
    c += c0
    e = np.full(1, c[refind] + op.ecore, dtype=pyci.c_double)
    c[refind] = 1
    return e, c[None, :]
