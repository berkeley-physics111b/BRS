import numpy as np

def beta_add_up_down_curves(upx, upy, downx, downy):
    """
    Corrects for magnetic hysteresis by shifting and averaging 'up' and 'down' 
    current scans to produce a single compensated spectrum.
    
    Args:
        upx, upy (array-like): Bin numbers (current) and counts for the upward scan.
        downx, downy (array-like): Bin numbers and counts for the downward scan.

    Returns:
        tuple: (xout, yout) where xout is the shifted x-axis (momenta) and 
               yout is the combined/summed count data.
    """
    # Convert to numpy arrays for vector operations
    upx, upy = np.array(upx).flatten(), np.array(upy).flatten()
    downx, downy = np.array(downx).flatten(), np.array(downy).flatten()
    
    # Identify the K-peak index for both directions
    idx_up = np.argmax(upy)
    idx_down = np.argmax(downy)
    
    maxup_x = upx[idx_up]
    maxdwn_x = downx[idx_down]
    
    # Calculate shift amount to align the peaks (midpoint strategy)
    shift_amt = (maxup_x - maxdwn_x) / 2
    
    # Shift the downward scan to the midpoint (goal)
    # The original MATLAB code shifts the vectors to align the datasets
    downx_shifted = downx + shift_amt
    
    # For a combined spectrum, we align the counts (assuming equivalent bin spacing)
    # In practice, you would interpolate; here we return shifted x and summed y
    return downx_shifted, upy + downy

def fk_transform(Z, momenta, counts):
    """
    Performs the Fermi-Kurie transform on the compensated beta spectrum.
    
    Args:
        Z (int): Atomic number of the daughter nucleus (e.g., 56 for Barium-137).
        momenta (array-like): Momentum values (p).
        counts (array-like): Compensated counts (N/I).

    Returns:
        tuple: (E, Y) where E is kinetic energy and Y is the FK value.
    """
    p = np.array(momenta)
    gamma = Z / 137.036
    S = np.sqrt(1 - gamma**2) - 1
    
    eta2 = p**2
    epsilon = np.sqrt(1 + eta2)
    
    # Fermi function approximation F(Z, p)
    # Applied as defined in the provided MATLAB script
    F = ((gamma**2) * (1 + eta2) + eta2 / 4)**S
    
    # Kinetic energy E = sqrt(p^2 + 1) - 1
    E = np.sqrt(eta2 + 1) - 1
    
    # Fermi-Kurie Y-axis: sqrt(N / (p^2 * F))
    # Where 'counts' has already been divided by p (current I)
    Y = np.sqrt(counts / (eta2 * F))
    
    return E, Y

def inverse_fk_transform(Z, E, FK_line_fit):
    """
    Reverses the FK transform to obtain the count contribution (Nc) 
    from a theoretical or fitted line.
    
    Args:
        Z (int): Atomic number.
        E (array-like): Kinetic energy values.
        FK_line_fit (array-like): Y-values from the FK linear fit.

    Returns:
        array: The theoretical compensated counts (Nc) for that component.
    """
    E = np.array(E)
    # Convert energy back to momentum p (eta)
    p = np.sqrt((E + 1)**2 - 1)
    eta2 = p**2
    
    gamma = Z / 137.036
    S = np.sqrt(1 - gamma**2) - 1
    F = ((gamma**2) * (1 + eta2) + eta2 / 4)**S
    
    # Reverse the FK relation: Y^2 * p^2 * F = counts
    # This yields the counts Nc needed for component subtraction
    Nc = (FK_line_fit**2) * (eta2 * F)
    
    return Nc