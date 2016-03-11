import argparse
import functools
import numpy as np
import os.path
import scipy.linalg as sla
import datetime
import os
import psutil

from pyspark import SparkContext, SparkConf

###################################
# Utility functions
###################################

def select_topr(vct_input, r):
    """
    Returns the R-th greatest elements indices
    in input vector and store them in idxs_n.
    """
    temp = np.argpartition(-vct_input, r)
    idxs_n = temp[:r]
    return idxs_n

def input_to_rowmatrix(raw_rdd, norm):
    """
    Utility function for reading the matrix data
    """
    # Parse each line of the input into a numpy array of floats. This requires
    # several steps.
    #  1: Split each string into a list of strings.
    #  2: Convert each string to a float.
    #  3: Convert each list to a numpy array.
    p_and_n = functools.partial(parse_and_normalize, norm = norm)
    numpy_rdd = raw_rdd \
        .zipWithIndex() \
        .map(lambda x: (x[1], p_and_n(x[0])))
    return numpy_rdd

###################################
# Spark helper functions
###################################

def parse_and_normalize(norm, line):
    """
    Utility function. Parses a line of text into a floating point array, then
    whitens the array.
    """
    x = np.array(map(float, line.strip().split()))

    # x.strip() -- strips off trailing whitespace from the string
    # .split("\t") -- splits the string into a list of strings, splitting on tabs
    # map(float, list) -- converts each element of the list from strings to floats
    # np.array(list) -- converts the list of floats into a numpy array

    if norm:
        x -= x.mean()  # 0-mean.
        x /= sla.norm(x)  # Unit norm.
    return x

def vector_matrix(row):
    """
    Applies u * S by row-wise multiplication, followed by a reduction on
    each column into a single vector.
    """
    # comment by Xiang: in this case there is T*log(T) complexity?
    # comment by Xiang: Also, whenever a "row_index, vector = row" is called,
    # there will be a reading on the portion of S on each node, right?

    row_index, vector = row     # Split up the [key, value] pair.
    u = _U_.value       # Extract the broadcasted vector "v".

    # Generate a list of [key, value] output pairs, one for each nonzero
    # element of vector.
    # comment by Xiang: the code below seems calculating all elements for
    # vector v, rather than only the nonzero elements;
    # comment by Xiang: also I'm puzzled why we are using the "append" function,
    # as the output of this should be of the same size?
    out = []
    for i in range(vector.shape[0]):
        out.append([i, u[row_index] * vector[i]])
    return out

def matrix_vector(row):
    """
    Applies S * v by row-wise multiplication. No reduction needed, as all the
    summations are performed within this very function.
    """
    k, vector = row
    # Extract the broadcast variables.
    v = _V_.value
    indices = _I_.value
    # Perform the multiplication using the specified indices in both arrays.
    innerprod = np.dot(vector[indices], v)
    # That's it! Return the [row, inner product] tuple.
    return [k, innerprod]

def deflate(row):
    """
    Deflates the data matrix by subtracting off the outer product of the
    broadcasted vectors and returning the modified row.
    """
    k, vector = row
    # It's important to keep order of operations in mind: we are computing
    # (and subtracting from S) the outer product of u * v. As we are operating
    # on a row-distributed matrix, we therefore will only iterate over the
    # elements of v, and use the single element of u that corresponds to the
    # index of the current row of S.
    # Got all that? Good! Explain it to me.
    u, v = _U_.value, _V_.value
    return [k, vector - (u[k] * v)]

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description = 'PySpark Dictionary Learning',
        add_help = 'How to use', prog = 'python R1DL_Spark.py <args>')

    # Inputs.
    parser.add_argument("-i", "--input", required = True,
        help = "Input file containing the matrix S.")
    parser.add_argument("-T", "--rows", type = int, required = True,
        help = "Number of rows (observations) in the input amtrix S.")
    parser.add_argument("-P", "--cols", type = int, required = True,
        help = "Number of columns (features) in the input amtrix S.")

    # Optional.
    parser.add_argument("-r", "--pnonzero", type = float, default = 0.07,
        help = "Percentage of non-zero elements. [DEFAULT: 0.07]")
    parser.add_argument("-m", "--dictatoms", type = int, default = 5,
        help = "Number of the dictionary atoms. [DEFAULT: 5]")
    parser.add_argument("-e", "--epsilon", type = float, default = 0.01,
        help = "The convergence criteria in the ALS step. [DEFAULT: 0.01]")
    parser.add_argument("--debug", action = "store_true",
        help = "If set, turns out debug output.")
    parser.add_argument("--normalize", action = "store_true",
        help = "If set, normalizes input data.")

    # Outputs.
    parser.add_argument("-d", "--dictionary", required = True,
        help = "Output path to dictionary file.(file_D)")
    parser.add_argument("-o", "--output", required = True,
        help = "Output path to z matrix.(file_z)")
    parser.add_argument("--prefix", required = True,
        help = "Prefix strings to the output files")

    args = vars(parser.parse_args())

    if args['debug']: print(datetime.datetime.now())

    # Initialize the SparkContext. This is where you can create RDDs,
    # the Spark abstraction for distributed data sets.
    sc = SparkContext(conf = SparkConf())

    # Read the data and convert it into a thunder RowMatrix.
    raw_rdd = sc.textFile(args['input'])
    S = input_to_rowmatrix(raw_rdd, args['normalize'])
    S.cache()

    ##################################################################
    # Here's where the real fun begins.
    #
    # First, we're going to initialize some variables we'll need for the
    # following operations. Next, we'll start the optimization loops. Finally,
    # we'll perform the stepping and deflation operations until convergence.
    #
    # Sound like fun?
    ##################################################################

    T = args['rows']
    P = args['cols']

    epsilon = args['epsilon']       # convergence stopping criterion
    M = args['dictatoms']            # dimensionality of the learned dictionary
    R = args['pnonzero'] * P        # enforces sparsity
    u_new = np.zeros(T)             # atom updates at each iteration
    v = np.zeros(P)

    indices_V = np.zeros(R)           # for top-R sorting

    max_iterations = P * 10
    file_D = os.path.join(args['dictionary'], "{}_D.txt".format(args["prefix"]))
    file_z = os.path.join(args['output'], "{}_z.txt".format(args["prefix"]))

    # Start the loop!
    for m in range(M):
        # Generate a random vector, subtract off its mean, and normalize it.
        u_old = np.random.random(T)
        u_old -= u_old.mean()
        u_old /= sla.norm(u_old)

        num_iterations = 0
        delta = 2 * epsilon

        # Start the inner loop: this learns a single atom.
        while num_iterations < max_iterations and delta > epsilon:
            # P2: Vector-matrix multiplication step. Computes v.
            _U_ = sc.broadcast(u_old)
            v = S \
                .flatMap(vector_matrix) \
                .reduceByKey(lambda x, y: x + y) \
                .collect()
            v = np.take(sorted(v), indices = 1, axis = 1)

            # Use our previous method to select the top R.
            indices_V = select_topr(v, R)

            # Broadcast the indices_V and the vector.
            _V_ = sc.broadcast(v[indices_V])
            _I_ = sc.broadcast(indices_V)

            # P1: Matrix-vector multiplication step. Computes u.
            u_new = S \
                .map(matrix_vector) \
                .collect()
            u_new = np.take(sorted(u_new), indices = 1, axis = 1)

            # Subtract off the mean and normalize.
            u_new -= u_new.mean()
            u_new /= sla.norm(u_new)

            # Update for the next iteration.
            delta = sla.norm(u_old - u_new)
            u_old = u_new
            num_iterations += 1

        # Save the newly-computed u and v to the output files;
        with open(file_D, "a+") as fD:
            np.savetxt(fD, u_new, fmt = "%.6f", newline = " ")
            fD.write("\n")
        temp_v = np.zeros(v.shape)
        temp_v[indices_V] = v[indices_V]
        v = temp_v
        with open(file_z, "a+") as fz:
            np.savetxt(fz, v, fmt = "%.6f", newline=" ")
            fz.write("\n")

        # P4: Deflation step. Update the primary data matrix S.
        _U_ = sc.broadcast(u_new)
        _V_ = sc.broadcast(v)

        if args['debug']: print(m)

        S = S.map(deflate).reduceByKey(lambda x, y: x + y)
        S.cache()

    # All done! Write out the matrices as tab-delimited text files, with
    # floating-point values to 6 decimal-point precision.
    if args['debug']: print(datetime.datetime.now())
    process = psutil.Process(os.getpid())
    print(process.memory_info().rss)
