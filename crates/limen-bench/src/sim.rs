//! A deterministic Monte-Carlo of the interference model (see `docs/paper/interference-model.md`).
//!
//! It checks the model's *qualitative* predictions numerically, with zero LLM spend, so the
//! framing can be falsified cheaply before any real run: (P1) interference grows super-linearly
//! in the number of writers; (P3/P5) cooperation recovers it, and full cooperation eliminates it.
//!
//! Model: one contended ("hot") region. Each agent touches it with probability
//! `q = 1 - (1-p)^e`. Under naive last-writer-wins, `k` writers on the region lose `k-1`
//! contributions. Under coordination, cooperating writers serialize losslessly; a non-cooperating
//! writer still clobbers. A tiny xorshift PRNG keeps every sweep reproducible without a dependency.

/// Deterministic xorshift64* PRNG (no external crate, so results are reproducible from a seed).
struct Rng(u64);

impl Rng {
    fn new(seed: u64) -> Self {
        Rng(seed | 1)
    }
    fn next_u64(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    /// Uniform in [0, 1).
    fn unit(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 / (1u64 << 53) as f64
    }
    fn bernoulli(&mut self, p: f64) -> bool {
        self.unit() < p
    }
}

#[derive(Clone, Copy, Debug)]
pub struct SimParams {
    /// number of concurrent writers
    pub n: usize,
    /// edits per agent
    pub e: usize,
    /// per-edit probability of touching the contended region (coupling)
    pub p: f64,
    /// fraction of writers that honor the coordinator (advisory adherence)
    pub alpha: f64,
    pub trials: usize,
    pub seed: u64,
}

#[derive(Clone, Copy, Debug, Default)]
pub struct SimStats {
    /// mean lost contributions per trial, uncoordinated
    pub lost_naive: f64,
    /// mean lost contributions per trial, coordinated at `alpha`
    pub lost_coord: f64,
    /// P(a run has no lost update), uncoordinated
    pub pass1_naive: f64,
    /// P(a run has no lost update), coordinated
    pub pass1_coord: f64,
    /// 1 - lost_coord / lost_naive
    pub recovered_fraction: f64,
}

pub fn simulate(params: &SimParams) -> SimStats {
    let q = 1.0 - (1.0 - params.p).powi(params.e as i32);
    let mut rng = Rng::new(params.seed);
    let (mut lost_n, mut lost_c) = (0.0_f64, 0.0_f64);
    let (mut clean_n, mut clean_c) = (0u64, 0u64);

    for _ in 0..params.trials {
        let mut writers = 0usize;
        let mut cooperating = 0usize;
        for _ in 0..params.n {
            if rng.bernoulli(q) {
                writers += 1;
                if rng.bernoulli(params.alpha) {
                    cooperating += 1;
                }
            }
        }
        // Naive: k writers on the hot region lose k-1 contributions.
        let lost_naive = writers.saturating_sub(1);
        lost_n += lost_naive as f64;
        if lost_naive == 0 {
            clean_n += 1;
        }
        // Coordinated: cooperating writers serialize losslessly among themselves; the
        // prevented collisions are (cooperating - 1), capped by what was at risk.
        let prevented = cooperating.saturating_sub(1).min(lost_naive);
        let lost_coord = lost_naive - prevented;
        lost_c += lost_coord as f64;
        if lost_coord == 0 {
            clean_c += 1;
        }
    }

    let t = params.trials as f64;
    let lost_naive = lost_n / t;
    let lost_coord = lost_c / t;
    SimStats {
        lost_naive,
        lost_coord,
        pass1_naive: clean_n as f64 / t,
        pass1_coord: clean_c as f64 / t,
        recovered_fraction: if lost_naive > 0.0 {
            1.0 - lost_coord / lost_naive
        } else {
            1.0
        },
    }
}

/// Parameters for the per-arm coupling sweep.
#[derive(Clone, Copy, Debug)]
pub struct ArmSweepParams {
    /// same-file (shared-region) coupling pairs
    pub n_shared: usize,
    /// cross-file (interface / write-skew) coupling pairs
    pub n_cross: usize,
    /// per-pair probability the coupling actually conflicts
    pub p: f64,
    /// advisory adherence — probability a reconciliation round fixes a cross-file conflict
    pub alpha: f64,
    pub trials: usize,
    pub seed: u64,
}

/// Pass rate by arm, plus the coupling fraction of the modeled task.
#[derive(Clone, Copy, Debug, Default)]
pub struct ArmPass {
    pub naive: f64,
    pub limen: f64,
    pub limen_deps: f64,
    pub coupling_fraction: f64,
}

/// Monte-Carlo of pass rate by arm as work shifts from same-file to cross-file coupling. Encodes
/// the mechanism the pilot showed: naive loses on any conflict; region leases (Limen) recover
/// same-file conflicts but not cross-file write skew; the advisory dependency round (LimenDeps)
/// recovers cross-file too, up to adherence `alpha`. A run passes an arm iff every conflicting
/// pair is handled by that arm.
pub fn simulate_arms(params: &ArmSweepParams) -> ArmPass {
    let mut rng = Rng::new(params.seed);
    let (mut pass_n, mut pass_l, mut pass_d) = (0u64, 0u64, 0u64);
    for _ in 0..params.trials {
        let (mut ok_n, mut ok_l, mut ok_d) = (true, true, true);
        for _ in 0..params.n_shared {
            if rng.bernoulli(params.p) {
                ok_n = false; // naive loses the same-file update; leases compose
            }
        }
        for _ in 0..params.n_cross {
            if rng.bernoulli(params.p) {
                ok_n = false; // naive fails
                ok_l = false; // per-file leases don't serialize cross-file skew
                if !rng.bernoulli(params.alpha) {
                    ok_d = false; // advisory round recovers it only when adhered to
                }
            }
        }
        pass_n += ok_n as u64;
        pass_l += ok_l as u64;
        pass_d += ok_d as u64;
    }
    let t = params.trials as f64;
    let total = (params.n_shared + params.n_cross) as f64;
    ArmPass {
        naive: pass_n as f64 / t,
        limen: pass_l as f64 / t,
        limen_deps: pass_d as f64 / t,
        coupling_fraction: if total > 0.0 {
            params.n_cross as f64 / total
        } else {
            0.0
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run(n: usize, alpha: f64) -> SimStats {
        simulate(&SimParams {
            n,
            e: 3,
            p: 0.2,
            alpha,
            trials: 30_000,
            seed: 42,
        })
    }

    #[test]
    fn interference_grows_superlinearly_in_writers() {
        // Quadratic-ish: doubling writers more than doubles lost updates (a linear law would
        // predict ~2x; the model predicts ~C(n,2) growth).
        let l2 = run(2, 1.0).lost_naive;
        let l4 = run(4, 1.0).lost_naive;
        assert!(l2 > 0.0);
        assert!(
            l4 > 2.5 * l2,
            "expected super-linear growth in N, got {l2:.4} -> {l4:.4}"
        );
    }

    #[test]
    fn full_cooperation_eliminates_loss() {
        let s = run(5, 1.0);
        assert!(s.lost_coord < 1e-9, "full cooperation should lose nothing");
        assert!(s.recovered_fraction > 0.999);
    }

    #[test]
    fn recovery_is_monotone_in_cooperation() {
        let r = |a: f64| run(5, a).recovered_fraction;
        assert!(r(1.0) >= r(0.5));
        assert!(r(0.5) >= r(0.0) - 1e-9);
        assert!(r(0.0).abs() < 0.05, "no cooperation recovers ~nothing");
    }

    fn arms(n_shared: usize, n_cross: usize, alpha: f64) -> ArmPass {
        simulate_arms(&ArmSweepParams {
            n_shared,
            n_cross,
            p: 0.6,
            alpha,
            trials: 30_000,
            seed: 7,
        })
    }

    #[test]
    fn arms_form_a_monotone_gradient() {
        // limen-deps >= limen >= naive everywhere along the sweep.
        for (s, c) in [(3, 0), (2, 1), (1, 2), (0, 3)] {
            let a = arms(s, c, 0.9);
            assert!(a.limen >= a.naive - 1e-9, "limen < naive at ({s},{c})");
            assert!(a.limen_deps >= a.limen - 1e-9, "deps < limen at ({s},{c})");
        }
    }

    #[test]
    fn region_leases_recover_same_file_but_not_cross_file() {
        // All same-file: leases recover everything (pass ~ 1), naive does not.
        let shared = arms(4, 0, 1.0);
        assert!(shared.limen > 0.99 && shared.naive < 0.9);
        // All cross-file: leases buy nothing over naive; the advisory round (alpha=1) recovers it.
        let cross = arms(0, 4, 1.0);
        assert!(
            (cross.limen - cross.naive).abs() < 0.02,
            "leases shouldn't help cross-file"
        );
        assert!(
            cross.limen_deps > 0.99,
            "advisory round should recover cross-file at alpha=1"
        );
    }

    #[test]
    fn advisory_recovery_scales_with_adherence() {
        let lo = arms(0, 4, 0.3).limen_deps;
        let hi = arms(0, 4, 0.9).limen_deps;
        assert!(
            hi > lo,
            "higher adherence should recover more cross-file: {lo:.3} !< {hi:.3}"
        );
    }
}
