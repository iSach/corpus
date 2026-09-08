# Seed paper sources

The seed set contains 34 real papers selected for the two research fields in `BUILD_PROMPT.md`: 17 papers on simulation-based inference and its reusable pretrained components, and 17 papers on diffusion-model composition, conditioning, inverse problems, and scientific applications. Metadata and abstracts were checked against the primary arXiv abstract pages listed below. The JSON abstracts are short paraphrases of the source abstracts; they are not verbatim paper text. Each record's PDF artifact points to the corresponding arXiv PDF. The code artifacts below point only to official author, lab, or project repositories whose README identifies the corresponding paper.

## Simulation-based inference and pretraining (`sbi-pretrain`)

1. [Masked Autoregressive Flow for Density Estimation](https://arxiv.org/abs/1705.07057) — normalizing-flow density estimation foundation used by neural likelihood and posterior estimators.
2. [Neural Spline Flows](https://arxiv.org/abs/1906.04032) — flexible invertible transforms for density estimation, variational inference, and SBI posterior models.
3. [Sequential Neural Likelihood: Fast Likelihood-free Inference with Autoregressive Flows](https://arxiv.org/abs/1805.07226) — adaptive neural likelihood training that reduces simulation cost.
4. [Automatic Posterior Transformation for Likelihood-Free Inference](https://arxiv.org/abs/1905.07488) — sequential neural posterior estimation under dynamically updated proposals.
5. [Likelihood-free MCMC with Amortized Approximate Ratio Estimators](https://arxiv.org/abs/1903.04057) — amortized ratio estimation for likelihood-free MCMC.
6. [SBI -- A toolkit for simulation-based inference](https://arxiv.org/abs/2007.09114) — practical software workflow bundling neural posterior, likelihood, and ratio estimators. Official code: [sbi-dev/sbi](https://github.com/sbi-dev/sbi).
7. [The frontier of simulation-based inference](https://arxiv.org/abs/1911.01429) — field overview connecting SBI methods to scientific inverse problems.
8. [BayesFlow: Amortized Bayesian Workflows With Neural Networks](https://arxiv.org/abs/2306.16015) — reusable simulation-trained compression and inference workflows. Official code: [bayesflow-org/bayesflow](https://github.com/bayesflow-org/bayesflow).
9. [Unifying Summary Statistic Selection for Approximate Bayesian Computation](https://arxiv.org/abs/2206.02340) — learned summaries and representation selection for likelihood-free inference.
10. [Neural Importance Sampling for Rapid and Reliable Gravitational-Wave Inference](https://arxiv.org/abs/2210.05686) — posterior proposals, importance correction, and reliability diagnostics in a scientific SBI application.
11. [Truncated proposals for scalable and hassle-free simulation-based inference](https://arxiv.org/abs/2210.04815) — robust sequential neural posterior estimation with truncated proposals and coverage tests.
12. [Simulation-based Inference for High-dimensional Data using Surjective Sequential Neural Likelihood Estimation](https://arxiv.org/abs/2308.01054) — learned dimension reduction for high-dimensional neural likelihood estimation.
13. [Real-time gravitational-wave science with neural posterior estimation](https://arxiv.org/abs/2106.12594) — amortized neural posterior estimation from simulated detector data.
14. [Calibrating Neural Simulation-Based Inference with Differentiable Coverage Probability](https://arxiv.org/abs/2310.13402) — differentiable calibration objective for neural SBI posteriors.
15. [Variational Inference with Coverage Guarantees in Simulation-Based Inference](https://arxiv.org/abs/2305.14275) — conformalized amortized variational posteriors with marginal coverage guarantees.
16. [Investigating the Impact of Model Misspecification in Neural Simulation-based Inference](https://arxiv.org/abs/2209.01845) — robustness and failure modes when simulations do not match observed data.
17. [Amortized In-Context Bayesian Posterior Estimation](https://arxiv.org/abs/2502.06601) — pretrained/amortized context-set posterior estimation and transfer evaluation.

## Diffusion composition and scientific applications (`diff-compose`)

1. [Denoising Diffusion Probabilistic Models](https://arxiv.org/abs/2006.11239) — core denoising-diffusion formulation and score-matching connection. Official code: [hojonathanho/diffusion](https://github.com/hojonathanho/diffusion).
2. [Denoising Diffusion Implicit Models](https://arxiv.org/abs/2010.02502) — faster non-Markovian and deterministic diffusion sampling. Official code: [ermongroup/ddim](https://github.com/ermongroup/ddim).
3. [Score-Based Generative Modeling through Stochastic Differential Equations](https://arxiv.org/abs/2011.13456) — SDE formulation unifying score-based and diffusion models. Official code: [yang-song/score_sde](https://github.com/yang-song/score_sde).
4. [Diffusion Models Beat GANs on Image Synthesis](https://arxiv.org/abs/2105.05233) — high-quality diffusion generation and classifier guidance. Official code: [openai/guided-diffusion](https://github.com/openai/guided-diffusion).
5. [Elucidating the Design Space of Diffusion-Based Generative Models](https://arxiv.org/abs/2206.00364) — modular design, preconditioning, and efficient diffusion training/sampling. Official code: [NVlabs/edm](https://github.com/NVlabs/edm).
6. [Classifier-Free Diffusion Guidance](https://arxiv.org/abs/2207.12598) — composable conditional and unconditional score guidance.
7. [Compositional Visual Generation with Composable Diffusion Models](https://arxiv.org/abs/2206.01714) — explicit composition of diffusion components as energy-based models. Official code: [energy-based-model/Compositional-Visual-Generation-with-Composable-Diffusion-Models-PyTorch](https://github.com/energy-based-model/Compositional-Visual-Generation-with-Composable-Diffusion-Models-PyTorch).
8. [Universal Guidance for Diffusion Models](https://arxiv.org/abs/2302.07121) — post-training arbitrary-modality guidance for pretrained diffusion models.
9. [Diffusion Posterior Sampling for General Noisy Inverse Problems](https://arxiv.org/abs/2209.14687) — posterior sampling for noisy linear and nonlinear inverse problems.
10. [Denoising Diffusion Restoration Models](https://arxiv.org/abs/2201.11793) — reuse of pretrained diffusion priors across linear restoration problems.
11. [Physics-Informed Diffusion Models](https://arxiv.org/abs/2403.14404) — first-principles constraints for physically valid generated samples.
12. [Diffusion model approach to simulating electron-proton scattering events](https://arxiv.org/abs/2310.16308) — diffusion-based event simulation for high-energy nuclear physics.
13. [GenCast: Diffusion-based ensemble forecasting for medium-range weather](https://arxiv.org/abs/2312.15796) — probabilistic diffusion forecasting for global weather ensembles.
14. [On conditional diffusion models for PDE simulations](https://arxiv.org/abs/2410.16415) — conditional and post-training conditioning for PDE forecasting and data assimilation.
15. [Score-based Data Assimilation](https://arxiv.org/abs/2306.10574) — score-model trajectory inference with observation guidance at inference time.
16. [Equivariant Diffusion for Molecule Generation in 3D](https://arxiv.org/abs/2203.17003) — E(3)-equivariant diffusion for local molecular geometry and atom types.
17. [MDM: Molecular Diffusion Model for 3D Molecule Generation](https://arxiv.org/abs/2209.05710) — force-aware equivariant diffusion with local molecular constraints.

No personal scores, proposed scores, review notes, or fictional metadata are included in the seed data. Those belong to later review/ingest workflows.
