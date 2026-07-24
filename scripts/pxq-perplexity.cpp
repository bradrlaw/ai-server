// Minimal, self-contained perplexity/NLL evaluator that links against the
// pxq_llama fork's libllama.so + libggml.so so it can load the fork's PXQ
// quants (which stock llama-perplexity cannot). The fork's ggml also runs
// standard quants (Q8_0/Q6_K/Q4_K_M), so running EVERY model through this one
// tool on the one engine gives a perfectly consistent perplexity comparison —
// any delta is purely the quant.
//
// Algorithm: matches llama.cpp / ik_llama.cpp canonical `llama-perplexity`.
// Contiguous non-overlapping windows of n_ctx tokens; each window is prefixed
// with a BOS at position 0, and only the SECOND HALF (positions >= n_ctx/2) is
// scored, using the first half as left-context. Per scored position k we sum
// the negative log-likelihood of predicting token[start+k+1] from logits[k].
// count = (n_ctx/2 - 1) per window. PPL = exp(sum_nll / count). This mirrors
// stock so absolute numbers are directly comparable AND cross-validate, while
// running every quant (incl. PXQ) through the one fork engine.
//
// Build: see scripts/pxq-perplexity-build.sh
#include "llama.h"
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

static std::string read_file(const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) { fprintf(stderr, "cannot open %s\n", path); exit(1); }
    fseek(f, 0, SEEK_END); long n = ftell(f); fseek(f, 0, SEEK_SET);
    std::string s; s.resize(n);
    if (fread(&s[0], 1, n, f) != (size_t)n) { fprintf(stderr, "read err\n"); exit(1); }
    fclose(f); return s;
}

int main(int argc, char **argv) {
    const char *model_path = nullptr, *text_path = nullptr;
    int n_ctx = 512, ngl = 999, max_chunks = 0;
    bool selftest = false;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "-m")) model_path = argv[++i];
        else if (!strcmp(argv[i], "-f")) text_path = argv[++i];
        else if (!strcmp(argv[i], "-c")) n_ctx = atoi(argv[++i]);
        else if (!strcmp(argv[i], "-ngl")) ngl = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--max-chunks")) max_chunks = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--selftest")) selftest = true;
    }
    if (!model_path) { fprintf(stderr, "usage: -m model [-f text -c ctx --max-chunks N] [--selftest]\n"); return 1; }

    llama_backend_init();
    llama_model_params mp = llama_model_default_params();
    mp.n_gpu_layers = ngl;
    llama_model *model = llama_model_load_from_file(model_path, mp);
    if (!model) { fprintf(stderr, "failed to load model\n"); return 1; }
    const int n_vocab = llama_n_vocab(model);
    fprintf(stderr, "loaded model, n_vocab=%d\n", n_vocab);

    llama_context_params cp = llama_context_default_params();
    cp.n_ctx = n_ctx; cp.n_batch = n_ctx; cp.n_ubatch = n_ctx;
    llama_context *ctx = llama_init_from_model(model, cp);
    if (!ctx) { fprintf(stderr, "failed to create context\n"); return 1; }

    if (selftest) {
        // ABI sanity: tokenize a short string, decode, print argmax next token id.
        const char *t = "The capital of France is";
        std::vector<llama_token> toks(64);
        int n = llama_tokenize(model, t, strlen(t), toks.data(), toks.size(), true, false);
        if (n < 0) { fprintf(stderr, "tokenize failed n=%d\n", n); return 1; }
        toks.resize(n);
        fprintf(stderr, "tokenized %d tokens:", n);
        for (int i = 0; i < n; i++) fprintf(stderr, " %d", toks[i]);
        fprintf(stderr, "\n");
        llama_batch b = llama_batch_init(n, 0, 1);
        for (int i = 0; i < n; i++) {
            b.token[i] = toks[i]; b.pos[i] = i;
            b.n_seq_id[i] = 1; b.seq_id[i][0] = 0; b.logits[i] = (i == n - 1);
        }
        b.n_tokens = n;
        if (llama_decode(ctx, b) != 0) { fprintf(stderr, "decode failed\n"); return 1; }
        float *lg = llama_get_logits_ith(ctx, n - 1);
        int best = 0; for (int v = 1; v < n_vocab; v++) if (lg[v] > lg[best]) best = v;
        fprintf(stderr, "argmax next token id = %d (logit %.3f)\n", best, lg[best]);
        printf("SELFTEST_OK argmax=%d\n", best);
        llama_batch_free(b);
        return 0;
    }

    if (!text_path) { fprintf(stderr, "need -f text for perplexity\n"); return 1; }
    // Qwen sets add_bos_token=false; match stock: only prefix BOS when the model wants it.
    const bool add_bos = llama_add_bos_token(model) > 0;
    fprintf(stderr, "add_bos=%d\n", (int)add_bos);
    std::string text = read_file(text_path);
    std::vector<llama_token> toks(text.size() + 16);
    int n = llama_tokenize(model, text.data(), text.size(), toks.data(), toks.size(), add_bos, false);
    if (n < 0) { toks.resize(-n); n = llama_tokenize(model, text.data(), text.size(), toks.data(), toks.size(), add_bos, false); }
    toks.resize(n);
    fprintf(stderr, "corpus tokens: %d\n", n);

    int n_chunks = n / n_ctx;
    if (max_chunks > 0 && n_chunks > max_chunks) n_chunks = max_chunks;
    const int first = n_ctx / 2;                 // canonical: score only 2nd half
    const llama_token bos = llama_token_bos(model);
    double nll = 0.0; long count = 0;
    llama_batch b = llama_batch_init(n_ctx, 0, 1);
    for (int c = 0; c < n_chunks; c++) {
        int start = c * n_ctx;
        llama_kv_cache_clear(ctx);
        for (int i = 0; i < n_ctx; i++) {
            // BOS at position 0 of every window (matches stock); only request
            // logits for the scored second half to halve softmax/gather work.
            b.token[i] = (i == 0 && add_bos) ? bos : toks[start + i];
            b.pos[i] = i;
            b.n_seq_id[i] = 1; b.seq_id[i][0] = 0;
            b.logits[i] = 1;   // request all; ordinal==position (avoids sparse-gather ambiguity)
        }
        b.n_tokens = n_ctx;
        if (llama_decode(ctx, b) != 0) { fprintf(stderr, "decode failed chunk %d\n", c); return 1; }
        // score positions [first, n_ctx-1): predict token[start+k+1] from logits[k].
        // Match canonical access: fetch the base pointer ONCE at `first` (only the
        // second half had logits requested, stored contiguously) and index from it,
        // rather than per-position get_logits_ith (ordinal vs position ambiguity).
        float *base = llama_get_logits_ith(ctx, 0);
        for (int k = first; k < n_ctx - 1; k++) {
            float *lg = base + (int64_t)k * n_vocab;
            float max = lg[0];
            for (int v = 1; v < n_vocab; v++) if (lg[v] > max) max = lg[v];
            double sum = 0.0;
            for (int v = 0; v < n_vocab; v++) sum += exp((double)(lg[v] - max));
            llama_token tgt = toks[start + k + 1];
            double logp = (double)(lg[tgt] - max) - log(sum);
            nll -= logp; count++;
        }
        if ((c + 1) % 10 == 0 || c + 1 == n_chunks)
            fprintf(stderr, "  chunk %d/%d  ppl=%.4f\n", c + 1, n_chunks, exp(nll / count));
    }
    llama_batch_free(b);
    double ppl = exp(nll / count);
    fprintf(stderr, "\n");
    printf("PPL %.5f  (chunks=%d ctx=%d predictions=%ld)\n", ppl, n_chunks, n_ctx, count);
    return 0;
}
