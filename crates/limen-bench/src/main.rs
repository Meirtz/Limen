//! `limen-bench` — runnable, compute-free apparatus demo.
//!
//! Prints (1) a sweep of the interference simulation and (2) the three arms on a
//! shared-region merge task. No LLMs, no network — deterministic synthetic numbers that
//! illustrate the phenomenon and exercise the real coordination code.

use limen_bench::arm::{Arm, ParLimen, ParNaive, ParPlacebo, Seq1};
use limen_bench::sim::{simulate, SimParams};
use limen_bench::task;

fn main() -> anyhow::Result<()> {
    // Inference-hub dev subcommands (need `INFERENCE_HUB_API_KEY`):
    //   `limen-bench models`                       — list available model ids
    //   `limen-bench complete <model> [prompt...]` — one-shot completion (validate a model)
    match std::env::args().nth(1).as_deref() {
        Some("models") => return list_models(),
        Some("complete") => return complete_cmd(),
        Some("pilot") => return pilot_cmd(),
        Some("analyze") => return analyze_cmd(),
        _ => {}
    }
    println!("# Interference simulation (synthetic, deterministic — NOT measured LLM results)\n");
    println!(
        "{:>3} {:>5} {:>12} {:>14} {:>12} {:>11}",
        "N", "p", "lost_naive", "pass@1_naive", "lost_coord", "recovered"
    );
    for &n in &[2usize, 3, 5, 8] {
        for &p in &[0.05_f64, 0.2, 0.5] {
            let s = simulate(&SimParams {
                n,
                e: 3,
                p,
                alpha: 1.0,
                trials: 50_000,
                seed: 1,
            });
            println!(
                "{:>3} {:>5.2} {:>12.3} {:>14.3} {:>12.3} {:>11.3}",
                n, p, s.lost_naive, s.pass1_naive, s.lost_coord, s.recovered_fraction
            );
        }
    }

    println!("\n# Mechanism demo — three arms on a shared-region merge task\n");
    let t = task::shared_region_merge();
    let arms: Vec<Box<dyn Arm>> = vec![
        Box::new(Seq1),
        Box::new(ParNaive),
        Box::new(ParPlacebo),
        Box::new(ParLimen),
    ];
    for arm in &arms {
        let r = arm.run(&t)?;
        println!(
            "{:>10}  passed={:<5}  lost_edit_lines={}  build_break={}  attribution={:?}",
            r.arm, r.passed, r.lost_edit_lines, r.build_break, r.attribution_correct
        );
    }
    Ok(())
}

/// `limen-bench models` — list available inference-hub model ids (needs `INFERENCE_HUB_API_KEY`),
/// so we can pin the exact model strings for the open-model pilot set before a run.
fn list_models() -> anyhow::Result<()> {
    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    rt.block_on(async {
        let client = limen_bench::model::ModelClient::from_env()?;
        let mut models = client.list_models().await?;
        models.sort();
        println!("{} models available on the inference hub:", models.len());
        for m in &models {
            println!("  {m}");
        }
        anyhow::Ok(())
    })
}

/// `limen-bench complete <model> [prompt...]` — a one-shot completion to validate a model
/// end-to-end (the POST `/chat/completions` path). Prints the reply.
fn complete_cmd() -> anyhow::Result<()> {
    use limen_bench::model::{ChatMessage, CompletionParams, ModelClient};
    let args: Vec<String> = std::env::args().skip(2).collect();
    let model = args
        .first()
        .cloned()
        .ok_or_else(|| anyhow::anyhow!("usage: limen-bench complete <model> [prompt...]"))?;
    let prompt = if args.len() > 1 {
        args[1..].join(" ")
    } else {
        "Reply with exactly: OK".to_string()
    };
    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    rt.block_on(async {
        let client = ModelClient::from_env()?;
        let out = client
            .complete(
                &model,
                &[ChatMessage::user(&prompt)],
                &CompletionParams {
                    temperature: 0.0,
                    max_tokens: 256,
                    seed: Some(1),
                },
            )
            .await?;
        println!("{out}");
        anyhow::Ok(())
    })
}

/// `limen-bench pilot <model-id> [more-model-ids...]` — run the real-agent pilot: every
/// (model × toy task × {naive, limen} × seed) cell, scored by the local executor, written to a
/// gitignored JSONL, with a pass-rate summary. Model ids are CLI args (never hardcoded).
/// `LIMEN_PILOT_SEEDS` (default 1) sets the seeds per cell.
fn pilot_cmd() -> anyhow::Result<()> {
    use limen_bench::exec::Executor;
    use limen_bench::model::{CompletionParams, ModelClient};
    use limen_bench::pilot;
    use limen_bench::runner::{append_jsonl, run_pilot, Coordination, PilotAgent};
    use std::collections::BTreeMap;

    let models: Vec<String> = std::env::args()
        .skip(2)
        .filter(|a| !a.starts_with("--"))
        .collect();
    if models.is_empty() {
        anyhow::bail!("usage: limen-bench pilot <model-id> [more-model-ids...]");
    }
    let seeds: u64 = std::env::var("LIMEN_PILOT_SEEDS")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(1);
    let temperature: f32 = std::env::var("LIMEN_PILOT_TEMP")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(0.0);
    let max_tokens: u32 = std::env::var("LIMEN_PILOT_MAX_TOKENS")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(2048);
    let out = std::path::PathBuf::from("results/pilot.jsonl");
    let short = |m: &str| m.rsplit('/').next().unwrap_or(m).to_string();

    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;
    rt.block_on(async {
        let client = ModelClient::from_env()?;
        let exec = Executor::Local;
        let tasks = pilot::all();
        // (model, task, coordination) -> (passes, total)
        let mut tally: BTreeMap<(String, String, String), (u32, u32)> = BTreeMap::new();

        for model in &models {
            for task in &tasks {
                for coord in [
                    Coordination::Naive,
                    Coordination::LimenPlacebo,
                    Coordination::Limen,
                    Coordination::LimenDeps,
                ] {
                    for seed in 1..=seeds {
                        let agent = PilotAgent::Model {
                            client: &client,
                            model: model.clone(),
                            params: CompletionParams {
                                temperature,
                                max_tokens,
                                seed: Some(seed),
                            },
                        };
                        match run_pilot(task, &agent, coord, &exec, seed).await {
                            Ok(run) => {
                                append_jsonl(&out, &run)?;
                                let cell = tally
                                    .entry((
                                        short(model),
                                        task.id.clone(),
                                        run.coordination.clone(),
                                    ))
                                    .or_default();
                                cell.1 += 1;
                                if run.passed {
                                    cell.0 += 1;
                                }
                                println!(
                                    "{:28} {:24} {:5} seed={seed} passed={}",
                                    short(model),
                                    task.id,
                                    run.coordination,
                                    run.passed
                                );
                            }
                            Err(err) => {
                                let cell = tally
                                    .entry((
                                        short(model),
                                        task.id.clone(),
                                        coord.label().to_string(),
                                    ))
                                    .or_default();
                                cell.1 += 1;
                                println!(
                                    "{:28} {:24} {:5} seed={seed} ERROR: {err}",
                                    short(model),
                                    task.id,
                                    coord.label()
                                );
                            }
                        }
                    }
                }
            }
        }

        println!("\n# Pass rate by (model, task, coordination) — results/pilot.jsonl\n");
        println!("{:28} {:24} {:5} {:>7}", "model", "task", "coord", "pass");
        for ((m, t, c), (pass, total)) in &tally {
            println!("{m:28} {t:24} {c:5} {pass:>3}/{total}");
        }
        anyhow::Ok(())
    })
}

/// `limen-bench analyze [results.jsonl]` — aggregate a JSONL run log into a per-(task,
/// coordination) pass-rate table with 95% Wilson intervals. Reads `results/pilot.jsonl` by
/// default. Pure post-processing — no network, no model.
fn analyze_cmd() -> anyhow::Result<()> {
    use anyhow::Context;
    use limen_bench::runner::PilotRun;
    use limen_bench::stats::tally_by;

    let path = std::env::args()
        .nth(2)
        .unwrap_or_else(|| "results/pilot.jsonl".to_string());
    let body = std::fs::read_to_string(&path).with_context(|| format!("reading {path}"))?;
    let runs: Vec<PilotRun> = body
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(serde_json::from_str)
        .collect::<Result<_, _>>()
        .with_context(|| format!("parsing run records in {path}"))?;
    if runs.is_empty() {
        println!("no runs in {path}");
        return Ok(());
    }

    let tallies = tally_by(
        runs.iter()
            .map(|r| ((r.task_id.clone(), r.coordination.clone()), r.passed)),
    );

    println!(
        "# Pass rate by (task, coordination) — {} runs from {path}\n",
        runs.len()
    );
    println!("| task | coordination | pass | rate | 95% CI |");
    println!("|---|---|---|---|---|");
    for ((task, coord), t) in &tallies {
        let (lo, hi) = t.ci95();
        println!(
            "| {task} | {coord} | {}/{} | {:.0}% | {:.0}–{:.0}% |",
            t.passes,
            t.n,
            t.rate() * 100.0,
            lo * 100.0,
            hi * 100.0
        );
    }
    Ok(())
}
