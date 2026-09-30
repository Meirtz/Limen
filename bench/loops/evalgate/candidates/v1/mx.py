def mx(xs):
    m = 0
    for x in xs:
        m = x if x > m else m
    return m
