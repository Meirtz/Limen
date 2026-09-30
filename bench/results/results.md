| set | scenario | kind | class | claim B>A | mlflow | sacred | limen | limen blocking findings |
|---|---|---|---|---|---|---|---|---|
| dev | fault_asymmetric_parse_errors | fault | asymmetric evaluation: control outputs cannot be parsed | yes | - | - | flag | ASYMMETRIC_NOT_EVALUATED |
| dev | fault_code_edit_between_arms | fault | confound: unrelated code edited between arms (uncommitted) | yes | - | flag | flag | CONFOUND |
| dev | fault_data_regenerated_tracked | fault | confound: training data regenerated between arms (tracked in git) | yes | - | flag | flag | CONFOUND |
| dev | fault_data_regenerated_untracked | fault | confound: training data regenerated between arms (not in git) | yes | - | - | flag | CONFOUND |
| dev | fault_eval_config_drift | fault | confound: evaluation config edited between arms | yes | flag | flag | flag | CONFOUND |
| dev | fault_gate_broken | fault | dead gate: check accepts anything | no | - | - | flag | GATE_CONTROLS_FAILED |
| dev | fault_gate_bypassed | fault | gate not executed: disabled by config | no | - | - | flag | GATE_NOT_EXECUTED |
| dev | fault_holdout_in_training | fault | leak: held-out test data read by training | yes | - | - | flag | HOLDOUT_READ |
| dev | fault_killed_replicate | fault | incomplete run used as evidence | yes | flag | flag | flag | CONFOUND, INCOMPLETE, ITEMS_DIFFER |
| dev | fault_selective_seed | fault | selective reporting: best treatment seed vs one control seed | no | - | - | - | - |
| dev | fault_stale_copy_shadow | fault | placebo: stale copy shadows the package | no | - | - | flag | PLACEBO, SHADOWED, TREATMENT_NOT_READ |
| dev | fault_treatment_typo | fault | placebo: treatment variable misspelled | no | - | - | flag | PLACEBO |
| dev | valid_candidates | valid | valid | yes | - | - | - | - |
| dev | valid_code_change | valid | valid | no | - | flag | - | - |
| dev | valid_features | valid | valid | yes | - | - | - | - |
| dev | valid_final_eval_on_holdout | valid | valid | yes | - | - | - | - |
| dev | valid_labels_and_outputs | valid | valid | yes | - | - | - | - |
| dev | valid_learning_rate | valid | valid | no | - | - | - | - |
| dev | valid_with_subprocess | valid | valid | yes | - | - | - | - |
| heldout | a1_eval_edge_tests_removed | fault | judge loosened for one arm: failing edge-case tests deleted before B was scored | yes | - | flag | flag | CONFOUND |
| heldout | a1_eval_missing_generations | fault | arms scored on different item sets: failed generations silently dropped from B | yes | - | - | flag | ITEMS_DIFFER |
| heldout | a1_eval_stale_verdict_cache | fault | treatment never measured: stale cached verdicts reused across candidate sets | no | - | - | flag | GATE_NOT_EXECUTED |
| heldout | a1_train_best_seed_reported | fault | selected evidence: best seed of the treatment vs mean of the control | yes | flag | flag | flag | CONFOUND |
| heldout | a1_train_leftover_env_var | fault | something else changed between arms: leftover environment variable in the B shell | yes | - | - | flag | CONFOUND |
| heldout | a1_train_local_rate_label_leak | fault | held-out leakage: feature computed from the evaluation split's own labels | yes | - | - | - | - |
| heldout | a2_evalgate_best_of_n_samples | fault | selective reporting: best of N sampled runs vs control mean | yes | flag | flag | flag | CONFOUND |
| heldout | a2_evalgate_missing_candidates_denominator | fault | arms scored on different item sets: failed generations excluded from denominator | yes | - | - | flag | ITEMS_DIFFER |
| heldout | a2_evalgate_nonstrict_quick_config | fault | evaluation cannot fail: correctness check disabled in one arm | yes | flag | flag | flag | CONFOUND, GATE_NOT_EXECUTED |
| heldout | a2_train_epochs_within_seed_noise | fault | noise mistaken for effect: difference within seed variance | yes | - | - | - | - |
| heldout | a2_train_features_value_typo | fault | treatment not applied: unrecognized setting value silently falls back to default | no | - | - | - | - |
| heldout | a2_train_threshold_tuned_on_val | fault | held-out leakage: threshold tuned on the evaluation split | yes | flag | flag | flag | CONFOUND |
| heldout | a3_appledouble_candidates | fault | junk files change one arm's item set (AppleDouble ._*.py) | no | - | - | flag | ASYMMETRIC_NOT_EVALUATED, ITEMS_DIFFER |
| heldout | a3_candidate_imports_answer_key | fault | held-out leakage: candidates read the judge's tests via the shared import path | yes | flag | flag | - | - |
| heldout | a3_local_config_override | fault | config precedence: leftover local override disables the gate | yes | flag | flag | flag | CONFOUND, GATE_NOT_EXECUTED |
| heldout | a3_snapshot_symlink_refresh | fault | data snapshot swapped between arms (symlinked 'current' pointer) | yes | - | - | flag | CONFOUND |
| heldout | a3_stale_bundle_shadow | fault | stale package copy shadows edited source (import path) | no | flag | flag | flag | PLACEBO, SHADOWED |
| heldout | a3_verdict_cache_stale_tests | fault | stale cache: verdicts from an older test suite reused by both arms | no | - | - | flag | GATE_NOT_EXECUTED |
| heldout | a1_eval_v2_shared_config_edit_valid | valid | none (uncommitted config edit applied before both arms; extra non-code file in v2) | yes | - | - | - | - |
| heldout | a1_train_quad_data_regen_valid | valid | none (data regenerated between arms, byte-identical) | yes | - | - | - | - |
| heldout | a1_train_x1sq_code_change_valid | valid | none (the code change between arms is the treatment) | yes | flag | flag | - | - |
| heldout | a2_evalgate_v2_config_reformatted | valid | none (config file rewritten between arms with identical settings) | yes | - | flag | flag | CONFOUND |
| heldout | a2_train_quad_final_test_eval | valid | none (held-out split read legitimately, for final evaluation in both arms) | yes | - | - | - | - |
| heldout | a2_train_quad_with_data_regen | valid | none (data files rewritten between arms, but deterministically identical) | yes | - | - | - | - |
| heldout | a3_valid_content_addressed_cache | valid | none (shared verdict cache, correctly keyed) | yes | - | - | flag | GATE_NOT_EXECUTED |
| heldout | a3_valid_precompiled_bytecode | valid | none (stale-looking __pycache__, timestamp-validated) | no | flag | flag | - | - |
| heldout | a3_valid_regen_data_identical | valid | none (data rewritten between arms, deterministically identical) | yes | - | - | - | - |
