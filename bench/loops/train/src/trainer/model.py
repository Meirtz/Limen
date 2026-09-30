import math
import random


def sigmoid(z):
    if z < -30:
        return 0.0
    if z > 30:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


def fit(examples, lr=0.1, epochs=30, l2=0.0, seed=0):
    rng = random.Random(seed)
    w = [0.0] * len(examples[0][1])
    order = list(range(len(examples)))
    for _ in range(epochs):
        rng.shuffle(order)
        for i in order:
            _, x, y = examples[i]
            p = sigmoid(sum(wi * xi for wi, xi in zip(w, x)))
            g = p - y
            w = [wi - lr * (g * xi + l2 * wi) for wi, xi in zip(w, x)]
    return w


def predict(w, x, threshold=0.5):
    return 1 if sigmoid(sum(wi * xi for wi, xi in zip(w, x))) >= threshold else 0
