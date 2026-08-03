suppressPackageStartupMessages(library("DESeq2"))

args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 7) {
  stop("expected counts, design, reference, comparison, result, normalized, and session paths")
}

counts_path <- args[[1]]
design_path <- args[[2]]
reference_condition <- args[[3]]
comparison_condition <- args[[4]]
result_path <- args[[5]]
normalized_path <- args[[6]]
session_path <- args[[7]]

counts <- read.delim(counts_path, check.names = FALSE, row.names = 1)
design <- read.delim(design_path, check.names = FALSE, stringsAsFactors = FALSE)
if (!identical(colnames(counts), design$sample)) {
  stop("count columns and sample design differ")
}
if (!setequal(unique(design$condition), c(reference_condition, comparison_condition))) {
  stop("declared conditions differ from sample design")
}
design$condition <- relevel(factor(design$condition), ref = reference_condition)
rownames(design) <- design$sample

dds <- DESeqDataSetFromMatrix(
  countData = as.matrix(counts),
  colData = design,
  design = ~ condition
)
dds <- dds[rowSums(counts(dds)) >= 10, ]
if (nrow(dds) < 10) {
  stop("fewer than ten expressed genes remain after count filtering")
}
dds <- DESeq(dds, quiet = TRUE)
res <- results(
  dds,
  contrast = c("condition", comparison_condition, reference_condition),
  alpha = 0.05
)
result <- data.frame(gene_id = rownames(res), as.data.frame(res), check.names = FALSE)
result <- result[order(result$padj, -abs(result$log2FoldChange), na.last = TRUE), ]
write.table(result, result_path, sep = "\t", quote = FALSE, row.names = FALSE)

normalized <- data.frame(
  gene_id = rownames(dds),
  as.data.frame(counts(dds, normalized = TRUE)),
  check.names = FALSE
)
write.table(normalized, normalized_path, sep = "\t", quote = FALSE, row.names = FALSE)

sink(session_path)
sessionInfo()
sink()
