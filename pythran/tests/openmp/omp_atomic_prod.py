def omp_atomic_prod():
    sum = 0
    LOOPCOUNT = 1000
    "omp parallel for"
    for i in xrange(LOOPCOUNT):
        "omp atomic"
        sum *= i
    return sum
