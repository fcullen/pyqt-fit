import numpy as np
from numpy.random import randint
from scipy import optimize
from collections import namedtuple
from itertools import izip
import kernel_smoothing
import sharedmem
import multiprocessing as mp
import bootstrap_workers

def adapt_curve_fit(fct, x, y, p0, args=(), **kwrds):
    popt, pcov = optimize.curve_fit(fct, x, y, **kwrds)
    return (popt, pcov, fct(popt, x, *args) - y)

def _percentile(array, p):
    n = (len(array)-1)*p
    n0 = np.floor(n)
    n1 = n0+1
    #print "%g percentile on %d = [%d-%d]" % (p*100, len(array), n0, n1)
    d = n-n0
    v0 = array[n0]
    v1 = array[n1]
    return v0 + d*(v1-v0)

def bootstrap_residuals(fct, xdata, ydata, repeats = 3000, residual = None, residuals = None, add_residual = None, correct_bias = False, **kwrds):
    """
    This implements the residual bootstrapping method for non-linear regression.

    Parameters
    ----------
    fct: callable
        Function evaluating the function on xdata at least with ``fct(xdata)``
    xdata: ndarray of shape (N,) or (k,N) for function with k predictors
        The independent variable where the data is measured
    ydata: ndarray
        The dependant data
    residuals: ndarray
        Residuals for the estimation on each xdata
    repeats: int
        Number of repeats for the bootstrapping
    add_residual: callable or None
        Function that add a residual to a value. The call ``add_residual(ydata,
        residual)`` should return the new ydata, with the residuals 'applied'. If
        None, it is considered the residuals should simply be added.
    correct_bias: boolean
        If true, the additive bias of the residuals is computed and restored
    kwrds: dict
        Dictionnary present to absorbed unknown named parameters

    Returns
    -------
    shuffled_x: ndarray
        Return xdata, with a new axis at position -2.
    shuffled_y: ndarray
        Return the shuffled ydata. There is a line per repeat, each line is shuffled independently.

    Notes
    -----
    TODO explain the method here, as well as how to create add_residual
    """
    if residuals is None:
        residuals = np.subtract

    yopt = fct(xdata)
    res = residuals(ydata, yopt)

    res -= np.mean(res)

    shuffle = randint(0, len(ydata), size=(repeats, len(ydata)))

    shuffled_res = res[shuffle]

    if correct_bias:
        kde = kernel_smoothing.LocalLinearKernel1D(xdata, res)
        bias = kde(xdata)
        shuffled_res += bias

    if add_residual is None:
        add_residual = np.add

    modified_ydata = add_residual(yopt,shuffled_res)

    return xdata[...,np.newaxis,:], modified_ydata

def bootstrap_regression(fct, xdata, ydata, residuals, repeats = 3000, **kwrds):
    """
    This implements the shuffling of standard bootstrapping method for non-linear regression.

    Parameters
    ----------
    fct: callable
        This is the function to optimize
    xdata: ndarray of shape (N,) or (k,N) for function with k predictors
        The independent variable where the data is measured
    ydata: ndarray
        The dependant data
    residuals: ndarray
        Residuals for the estimation on each xdata
    repeats: int
        Number of repeats for the bootstrapping
    kwrds: dict
        Dictionnary to absorbed unknown named parameters

    Returns
    -------
    shuffled_x: ndarray
        Return the shuffled x data. The axis -2 has one element per repeat, the other axis are shuffled independently.
    shuffled_y: ndarray
        Return the shuffled ydata. There is a line per repeat, each line is shuffled independently.

    Notes
    -----
    TODO explain the method here
    """
    shuffle = randint(0, len(ydata), size=(repeats, len(ydata)))
    shuffled_x = xdata[...,shuffle]
    shuffled_y = ydata[shuffle]
    return shuffled_x, shuffled_y

def getCIs(CI, *arrays):
    sorted_arrays = [ np.sort(a, axis=0) for a in arrays ]

    if not np.iterable(CI):
        CI = (CI,)

    CIs = tuple(np.zeros((len(CI), 2,)+a.shape[1:], dtype=float) for a in arrays)
    for i, ci in enumerate(CI):
        ci = (1-ci/100.0)/2
        for cis, sorted_array in izip(CIs, sorted_arrays):
            low = _percentile(sorted_array, ci)
            high = _percentile(sorted_array, 1-ci)
            cis[i] = [low, high]

    return CIs

BootstrapResult = namedtuple('BootstrapResult', 'y_fit y_est y_eval CIs shuffled_xs shuffled_ys full_results')

def bootstrap(fit, xdata, ydata, CI, shuffle_method = bootstrap_residuals, shuffle_args = (), shuffle_kwrds = {}, repeats = 3000, eval_points = None, full_results = False, nb_workers = None, extra_attrs = (), fit_args=(), fit_kwrds={}):
    """
    fit: callable
        Method used to compute regression. The call is:
            ``f = fit(xdata, ydata, *fit_args, **fit_kwrds)``
        Fit should return an object that would evaluate the regression on a set of points. The next call will be:
            ``f(eval_points)``
    xdata: ndarray of shape (N,) or (k,N) for function with k predictors
        The independent variable where the data is measured
    ydata: ndarray
        The dependant data
    CI: tuple of float
        List of percentiles to extract
    shuffle_method: callable
        Create shuffled dataset. The call is:
        ``shuffle_method(xdata, ydata, y_est, repeat=repeats, *shuffle_args, **shuffle_kwrds)``
        where ``y_est`` is the estimated dependant variable on the xdata.
    shuffle_args: tuple
        List of arguments for the shuffle method
    shuffle_kwrds: dict
        Dictionnary of arguments for the shuffle method
    repeats: int
        Number of repeats for the bootstraping
    eval_points: ndarray or None
        List of points to evaluate. If None, eval_point is xdata.
    full_results: bool
        if True, output also the whole set of evaluations
    extra_attrs: tuple of str
        List of attributes of the fitting method to extract on top of the y values for confidence intervals
    fit_args: tuple
        List of extra arguments for the fit callable
    fit_kwrds: dict
        Dictionnary of extra named arguments for the fit callable

    :Returns:
        y_est: ndarray
            Y estimated on xdata
        y_est: ndarray
            Y estimated on eval_points
        CIs: ndarray
            list of estimated confidence interval for each value of eval_points
        shuffled_xs: ndarray
            if full_results is True, the shuffled x's used for the bootstrapping
        shuffled_ys: ndarray
            if full_results is True, the shuffled y's used for the bootstrapping
        full_results: ndarray
            if full_results is True, the estimated y's for each shuffled_ys
    """
    y_fit = fit(xdata, ydata, *fit_args, **fit_kwrds)
    shuffled_x, shuffled_y = shuffle_method(y_fit, xdata, ydata, repeats=repeats, *shuffle_args, **shuffle_kwrds)
    nx = shuffled_x.shape[-2]
    ny = shuffled_y.shape[0]
    extra_values = []
    for attr in extra_attrs:
        extra_values.append(getattr(y_fit, attr))

    if eval_points is None:
        eval_points = xdata
    if nb_workers is None:
        nb_workers = mp.cpu_count()

    multiprocess = nb_workers > 1

# Copy everything in shared mem
    if multiprocess:
        ra = sharedmem.zeros((repeats+1, len(eval_points)), dtype=float)
        result_array = ra.np
        sx = sharedmem.array(shuffled_x)
        sy = sharedmem.array(shuffled_y)
        ep = sharedmem.array(eval_points)
        eas = [ sharedmem.zeros((repeats+1, len(ev)), dtype=float) for ev in extra_values ]
        extra_arrays = [ ea.np for ea in eas ]

        pool = mp.Pool(mp.cpu_count(), bootstrap_workers.initialize_shared, ( nx, ny, ra, eas, sx, sy, ep, extra_attrs, fit, fit_args, fit_kwrds))
    else:
        result_array = np.empty((repeats+1, len(eval_points)), dtype=float)
        bootstrap_workers.initialize(nx, ny, result_array, extra_arrays, shuffled_x, shuffled_y, eval_points, extra_attrs, fit, fit_args, fit_kwrds)
        extra_arrays = [ np.empty((repeats+1, len(ev)), dtype=float) for ev in extra_values ]

    result_array[0] = y_fit(eval_points)

    for ea, ev in izip(extra_arrays, extra_values):
        ea[0] = ev

    base_repeat = repeats / nb_workers
    if base_repeat*nb_workers < repeats:
        base_repeat += 1

    for i in xrange(nb_workers):
        end_repeats = (i+1)*base_repeat
        if end_repeats > repeats:
            end_repeats = repeats
        if multiprocess:
            pool.apply_async(bootstrap_workers.bootstrap_result, (i, i*base_repeat, end_repeats))
        else:
            bootstrap_workers.bootstrap_result(i, i*base_repeat, end_repeats)

    if multiprocess:
        pool.close()
        pool.join()
    CIs = getCIs(CI, result_array, *extra_arrays)

    y_eval = np.array(result_array[0]) # copy the array to not return a view on a larger array

    if not full_results:
        shuffled_y = shuffled_x = result_array = None
        extra_arrays = ()
    elif multiprocess:
        result_array = result_array.copy() # copy in local memory
        extra_arrays = [ ea.copy for ea in extra_arrays ]

    return BootstrapResult(y_fit, y_fit(xdata), y_eval, CIs, shuffled_x, shuffled_y, result_array)

def test():
    import cyth
    import quad
    from numpy.random import rand, randn
    from pylab import plot, savefig, clf, legend, arange, figure, title, show
    from curve_fitting import curve_fit
    import residuals

    def quadratic(x,(p0,p1,p2)):
        return p0 + p1*x + p2*x**2
    #test = quadratic
    test = quad.quadratic

    init = (10,1,1)
    target = np.array([10,4,1.2])
    print "Target parameters: %s" % (target,)
    x = 6*rand(200) - 3
    y = test(x, target)*(1+0.3*randn(x.shape[0]))
    xr = arange(-3, 3, 0.01)
    yr = test(xr,target)

    print "Estimage best parameters, fixing the first one"
    popt, pcov, _, _ = curve_fit(test, x, y, init, fix_params=(0,))
    print "Best parameters: %s" % (popt,)

    print "Estimate best parameters from data"
    popt, pcov, _, _ = curve_fit(test, x, y, init)
    print "Best parameters: %s" % (popt,)

    figure(1)
    clf()
    plot(x, y, '+', label='data')
    plot(xr, yr, 'r', label='function')
    legend(loc='upper left')

    print "Residual bootstrap calculation"
    result_r = bootstrap_fit(test, x, y, init, (95, 99), shuffle_method=bootstrap_residuals, eval_points = xr, fit=curve_fit)
    popt_r, pcov_r, res_r, CI_r, CIp_r, extra_r = result_r
    yopt_r = test(xr, popt_r)

    figure(2)
    clf()
    plot(xr, yopt_r, 'g', label='estimate')
    plot(xr, yr, 'r', label='target')
    plot(xr, CI_r[0][0], 'b--', label='95% CI')
    plot(xr, CI_r[0][1], 'b--')
    plot(xr, CI_r[1][0], 'k--', label='99% CI')
    plot(xr, CI_r[1][1], 'k--')
    legend(loc='upper left')
    title('Residual Bootstrapping')

    print "Regression bootstrap calculation"
    popt_c, pcov_c, res_c, CI_c, CIp_r, extra_c = bootstrap_fit(test, x, y, init, CI=(95, 99), shuffle_method=bootstrap_regression, eval_points = xr, fit=curve_fit)
    yopt_c = test(xr, popt_c)

    figure(3)
    clf()
    plot(xr, yopt_c, 'g', label='estimate')
    plot(xr, yr, 'r', label='target')
    plot(xr, CI_c[0][0], 'b--', label='95% CI')
    plot(xr, CI_c[0][1], 'b--')
    plot(xr, CI_c[1][0], 'k--', label='99% CI')
    plot(xr, CI_c[1][1], 'k--')
    legend(loc='upper left')
    title('Regression Bootstrapping (also called Case Resampling)')

    print "Done"

    show()

    return locals()

def profile(filename = 'bootstrap_profile'):
    import cProfile
    import pstats
    cProfile.run('res = bootstrap.test()', 'bootstrap_profile')
    p = pstats.Stats('bootstrap_profile')
    return p

if __name__ == "__main__":
    test()

