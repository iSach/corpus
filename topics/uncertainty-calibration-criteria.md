# Uncertainty calibration: relevance criteria

Identify work that helps decide whether a model's predicted probabilities or intervals can be trusted, and how to improve them when they cannot. The central interest is calibration under distribution shift: methods that estimate or correct miscalibration, and evaluations that measure it on held-out or shifted data.

A paper qualifies as a method to investigate, direct evaluation evidence, or supporting work with a concrete research use. Supporting work can include benchmarks, datasets with natural shifts, calibration metrics, and strong baselines. Positive and negative findings can be equally relevant.

Relevance: high for central methods or direct evidence; medium for concretely useful supporting work; low for indirect background; exclude when no research connection is identifiable. Relevance is separate from paper quality, model performance, and assessment confidence.

Ask:
1. What is calibrated: class probabilities, regression intervals, or generated outputs?
2. Is calibration fixed after training, fitted on held-out data, or adapted at test time?
3. What shift is evaluated: none, synthetic corruptions, natural dataset shift, or a new domain?
4. What measures calibration: ECE and its variants, proper scoring rules, interval coverage, or reliability diagrams? Is the binning or estimator reported?
5. Are improvements separated from accuracy changes, and compared against temperature scaling or another simple baseline?

Abstract-only assessments use only the matching title, abstract, and metadata. Missing details remain unknown. Do not borrow full-text findings or equate omission with absence. Retain potentially central candidates for checking when uncertainty matters.

Full-text checks name the inspected sections, verify the important inputs, shifts, and metrics, and preserve the original abstract assessment. State what was confirmed, changed, or remains unresolved. Use matching source versions; explicitly flag unknown versions.
