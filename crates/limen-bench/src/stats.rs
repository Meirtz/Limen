//! Small, pure statistics for summarizing pilot runs. Coordination-independent: it only counts
//! pass/fail outcomes, so it can never make one arm look better "by construction".

use std::collections::BTreeMap;

/// 95% Wilson score interval for `passes` successes out of `n`. Returns `(lo, hi)` clamped to
/// `[0, 1]`; `(0.0, 0.0)` when `n == 0`. The Wilson interval is well-behaved at the extremes
/// (0% / 100%) where the normal approximation degenerates — important for small pilots.
pub fn wilson_95(passes: u64, n: u64) -> (f64, f64) {
    if n == 0 {
        return (0.0, 0.0);
    }
    let z = 1.959963984540054_f64; // 97.5th percentile of the standard normal
    let nf = n as f64;
    let phat = passes as f64 / nf;
    let z2 = z * z;
    let denom = 1.0 + z2 / nf;
    let center = phat + z2 / (2.0 * nf);
    let margin = z * ((phat * (1.0 - phat) / nf) + z2 / (4.0 * nf * nf)).sqrt();
    (
        ((center - margin) / denom).max(0.0),
        ((center + margin) / denom).min(1.0),
    )
}

/// A pass/total tally with its rate and confidence interval.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Tally {
    pub n: u64,
    pub passes: u64,
}

impl Tally {
    pub fn rate(self) -> f64 {
        if self.n == 0 {
            0.0
        } else {
            self.passes as f64 / self.n as f64
        }
    }
    pub fn ci95(self) -> (f64, f64) {
        wilson_95(self.passes, self.n)
    }
}

/// Aggregate `(key, passed)` rows into a per-key [`Tally`], ordered by key.
pub fn tally_by<K: Ord, I: IntoIterator<Item = (K, bool)>>(rows: I) -> BTreeMap<K, Tally> {
    let mut m: BTreeMap<K, Tally> = BTreeMap::new();
    for (k, passed) in rows {
        let t = m.entry(k).or_default();
        t.n += 1;
        if passed {
            t.passes += 1;
        }
    }
    m
}

#[cfg(test)]
mod tests {
    use super::*;

    fn close(a: f64, b: f64) -> bool {
        (a - b).abs() < 0.01
    }

    #[test]
    fn wilson_handles_extremes_and_midpoints() {
        assert_eq!(wilson_95(0, 0), (0.0, 0.0));
        let (lo, hi) = wilson_95(5, 5); // all pass
        assert!(close(lo, 0.566) && close(hi, 1.0), "{lo},{hi}");
        let (lo, hi) = wilson_95(0, 5); // none pass
        assert!(close(lo, 0.0) && close(hi, 0.434), "{lo},{hi}");
        let (lo, hi) = wilson_95(1, 2); // 50%
        assert!(close(lo, 0.094) && close(hi, 0.906), "{lo},{hi}");
    }

    #[test]
    fn tally_of_nothing_is_empty() {
        let empty: Vec<((&str, &str), bool)> = vec![];
        assert!(tally_by(empty).is_empty());
        // and a wider interval for fewer samples
        let (_, hi5) = wilson_95(5, 5);
        let (_, hi50) = wilson_95(50, 50);
        assert!(
            hi50 < hi5 + 1e-9,
            "more samples should not widen the upper bound"
        );
    }

    #[test]
    fn tally_groups_and_counts() {
        let rows = vec![
            (("t", "naive"), false),
            (("t", "naive"), true),
            (("t", "limen"), true),
            (("t", "limen"), true),
        ];
        let m = tally_by(rows);
        assert_eq!(m[&("t", "naive")], Tally { n: 2, passes: 1 });
        assert_eq!(m[&("t", "limen")], Tally { n: 2, passes: 2 });
        assert_eq!(m[&("t", "limen")].rate(), 1.0);
    }
}
