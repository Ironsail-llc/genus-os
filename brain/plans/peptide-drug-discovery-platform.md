# Peptide Drug Discovery Platform — Master Plan

**Created:** 2026-03-21
**Status:** Planning Phase
**Owner:** Philip D'Agostino (Chief Scientist, R&D)

---

## Vision

Build a computational platform for designing peptide therapeutics for any disease with a protein target. Start with cancer vaccines (Conyngham's playbook), expand to metabolic disorders, infectious disease, aging/longevity.

---

## The Pipeline

```
┌─────────────────────────────────────────────────────────────┐
│  PHASE 1: MECHANISM DISCOVERY                               │
│  "What proteins cause this disease?"                        │
│  └── LitMaps, Elicit, KEGG, Reactome, STRING, GWAS         │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  PHASE 2: TARGET VALIDATION                                 │
│  "Is this protein druggable?"                               │
│  └── AlphaFold DB, DiffDock, binding site prediction        │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  PHASE 3: PEPTIDE DESIGN                                    │
│  "Design a peptide that binds this target"                  │
│  └── RFdiffusion, ProteinMPNN, LigandMPNN                   │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  PHASE 4: OPTIMIZATION                                      │
│  "Make it stable and effective"                             │
│  └── LinearDesign, RNAfold, PeptideRanker, ADMETlab         │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  PHASE 5: SYNTHESIS & TESTING                               │
│  "Make it and test it"                                      │
│  └── GenScript, TriLink, academic partners                  │
└─────────────────────────────────────────────────────────────┘
```

---

## Tool Categories

### 1. Sequencing & QC
- FastQC, fastp, Trimmomatic, Cutadapt

### 2. Alignment
- BWA-MEM2 (DNA), STAR (RNA), Bowtie2, Minimap2

### 3. Post-Processing
- SAMtools, Picard, GATK

### 4. Variant Calling
- GATK Mutect2 (somatic), FreeBayes, Strelka2

### 5. HLA Typing
- OptiType, xHLA, Kourami

### 6. Neoantigen Prediction
- pVACtools, NetMHCpan, IEDB, MHCflurry

### 7. Protein Structure
- AlphaFold 3, AlphaFold DB, RoseTTAFold2, ESMFold

### 8. Peptide-Protein Docking
- DiffDock, AutoDock Vina, Rosetta, GNINA

### 9. Peptide Design
- RFdiffusion, ProteinMPNN, LigandMPNN, PeptideBuilder

### 10. mRNA Design
- LinearDesign, RNAfold, NeoDesign

### 11. Peptide Property Prediction
- PeptideRanker, ToxinPred, PepFold, ADMETlab

### 12. Mechanism Discovery
- LitMaps, Elicit, KEGG, Reactome, STRING, GWAS Catalog

### 13. Pipeline Orchestration
- Nextflow, Cromwell + WDL, Snakemake

### 14. Sequencing Services
- Novogene, Genewiz, UNSW Ramaciotti, Element Biosciences

### 15. Peptide Synthesis
- GenScript, Peptides International, Bachem

### 16. mRNA Synthesis
- TriLink, Aldevron, GenScript, Academic RNA institutes

---

## Learning Tasks (Week 1-4)

| Task | Priority | Deadline |
|------|----------|----------|
| Learn: AlphaFold 3 Structure Prediction | 🔴 High | Week 1 |
| Learn: pVACtools Neoantigen Pipeline | 🔴 High | Week 1-2 |
| Learn: DiffDock Molecular Docking | 🔴 High | Week 2 |
| Learn: RFdiffusion Peptide Design | 🔴 High | Week 2-3 |
| Learn: Mechanism Discovery Tools | 🔴 High | Week 1-2 |

---

## Deployment Tasks (Week 3-6)

| Task | Priority | Deadline |
|------|----------|----------|
| Deploy: Nextflow Pipeline Infrastructure | 🟡 Normal | Week 3-4 |
| Research: Sequencing & Synthesis Partners | 🟡 Normal | Week 3-4 |

---

## Quick Access Links

### Structure Prediction
- AlphaFold Server: https://alphafoldserver.com
- AlphaFold DB: https://alphafold.ebi.ac.uk

### Mechanism Discovery
- LitMaps: https://litmaps.com
- Elicit: https://elicit.com
- KEGG: https://www.genome.jp/kegg/
- Reactome: https://reactome.org/
- STRING: https://string-db.org/
- GWAS Catalog: https://www.ebi.ac.uk/gwas/

### Neoantigen Prediction
- pVACtools: https://pvactools.readthedocs.io/
- NetMHCpan: https://services.healthtech.dtu.dk/service.php?NetMHCpan-4.1
- IEDB: http://tools.iedb.org/

### Peptide Design
- RFdiffusion: https://github.com/RosettaCommons/RFdiffusion
- ProteinMPNN: https://github.com/dauparas/ProteinMPNN

### Docking
- DiffDock: https://github.com/gcorso/DiffDock

### Property Prediction
- PeptideRanker: http://distilldeep.ucd.ie/PeptideRanker/
- ADMETlab: https://admet.scbdd.com/

### Sequencing Services
- Novogene: https://www.novogene.com/
- Genewiz: https://www.genewiz.com/

### Peptide Synthesis
- GenScript: https://www.genscript.com/
- Bachem: https://www.bachem.com/

### mRNA Synthesis
- TriLink: https://www.trilinkbiotech.com/
- Aldevron: https://www.aldevron.com/

---

## Cost Estimates

### Computational Infrastructure
| Component | Monthly Cost |
|-----------|--------------|
| Compute (32-64 cores, 256GB RAM) | $500-1,500 |
| GPU (A100 on-demand for AlphaFold) | $1,500-3,000 |
| Storage (5TB SSD) | $100-300 |
| **Total** | **$2,100-4,800/month** |

### Per-Patient Costs (One-Off)
| Service | Cost |
|---------|------|
| Sequencing (WES paired tumor/normal) | ~$3,000 |
| mRNA synthesis + LNP | ~$5,000-15,000 |
| Administration | ~$500-1,000 |
| **Total per patient** | **$8,500-19,000** |

### At Scale (100+ patients)
| Service | Cost |
|---------|------|
| Sequencing | ~$1,000-1,500 |
| mRNA synthesis + LNP | ~$1,000-3,000 |
| **Total per patient** | **$2,000-4,500** |

---

## Revenue Model

| Stream | Price Point | Margin |
|--------|-------------|--------|
| Veterinary cancer vaccine | $10,000-15,000/treatment | 40-60% |
| Human compassionate use | $25,000-50,000/treatment | 50-70% |
| Pipeline as a Service (B2B) | $100K-500K/year | 80%+ |
| Custom peptide design | $50K-200K/project | 70%+ |
| IP licensing | 5-10% royalty | 90%+ |

---

## Next Steps

1. **Week 1:** Start learning AlphaFold 3 + mechanism discovery tools
2. **Week 2:** Learn pVACtools + DiffDock
3. **Week 3:** Deploy Nextflow pipeline + research partners
4. **Week 4:** Run proof-of-concept on public data
5. **Week 5-8:** First veterinary case

---

## References

- Conyngham's dog Rosie: First personalized cancer vaccine designed by one person
- AlphaFold 3: Nobel Prize-winning protein structure prediction
- pVACtools: Standard neoantigen prediction pipeline
- RFdiffusion: State-of-the-art protein/peptide design

---

*Last updated: 2026-03-21*
