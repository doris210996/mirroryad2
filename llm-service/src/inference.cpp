#include "inference.h"
#include "prompt.h"
#include "llama.h"
#include "common.h"
#include <iostream>
#include <sstream>
#include <algorithm>

InferenceEngine::InferenceEngine() = default;

InferenceEngine::~InferenceEngine() {
    if (model_) {
        llama_model_free(model_);
        model_ = nullptr;
    }
}

bool InferenceEngine::load_model(const std::string& model_path, int n_ctx, int n_threads) {
    n_ctx_ = n_ctx;
    n_threads_ = n_threads;

    auto mparams = llama_model_default_params();
    mparams.n_gpu_layers = 0;  // CPU only

    std::cout << "[LLM] Loading model from " << model_path << " ..." << std::endl;
    model_ = llama_model_load_from_file(model_path.c_str(), mparams);
    if (!model_) {
        std::cerr << "[LLM] Failed to load model" << std::endl;
        return false;
    }
    std::cout << "[LLM] Model loaded successfully" << std::endl;
    return true;
}

SentimentResult InferenceEngine::analyze(const std::string& description) {
    std::lock_guard<std::mutex> lock(inference_mutex_);

    if (!model_) {
        return {"neutral", 0.0f, {}};
    }

    // Create context for this inference
    auto cparams = llama_context_default_params();
    cparams.n_ctx = n_ctx_;
    cparams.n_threads = n_threads_;
    cparams.n_threads_batch = n_threads_;

    auto* ctx = llama_init_from_model(model_, cparams);
    if (!ctx) {
        std::cerr << "[LLM] Failed to create context" << std::endl;
        return {"neutral", 0.0f, {}};
    }

    // Build prompt
    std::string prompt = build_sentiment_prompt(description);

    // Tokenize
    const auto* vocab = llama_model_get_vocab(model_);
    int n_prompt_max = prompt.size() + 128;
    std::vector<llama_token> tokens(n_prompt_max);
    int n_tokens = llama_tokenize(vocab, prompt.c_str(), prompt.size(),
                                   tokens.data(), n_prompt_max, true, true);
    if (n_tokens < 0) {
        std::cerr << "[LLM] Tokenization failed" << std::endl;
        llama_free(ctx);
        return {"neutral", 0.0f, {}};
    }
    tokens.resize(n_tokens);

    // Generate response tokens (max 256 tokens for JSON output)
    const int max_gen = 256;

    // Evaluate prompt
    auto batch = llama_batch_init(n_tokens + max_gen, 0, 1);
    for (int i = 0; i < n_tokens; i++) {
        common_batch_add(batch, tokens[i], i, {0}, false);
    }
    batch.logits[batch.n_tokens - 1] = true;

    if (llama_decode(ctx, batch) != 0) {
        std::cerr << "[LLM] Failed to decode prompt" << std::endl;
        llama_batch_free(batch);
        llama_free(ctx);
        return {"neutral", 0.0f, {}};
    }
    std::string output;
    auto* sampler = llama_sampler_chain_init(llama_sampler_chain_default_params());
    llama_sampler_chain_add(sampler, llama_sampler_init_temp(0.1f));
    llama_sampler_chain_add(sampler, llama_sampler_init_dist(42));

    int n_cur = batch.n_tokens;

    for (int i = 0; i < max_gen; i++) {
        llama_token new_token = llama_sampler_sample(sampler, ctx, -1);

        if (llama_vocab_is_eog(vocab, new_token)) break;

        char buf[256];
        int n = llama_token_to_piece(vocab, new_token, buf, sizeof(buf), 0, false);
        if (n > 0) {
            // Ensure we only append valid UTF-8
            output.append(buf, n);
        }

        // Check for end-of-turn marker
        if (output.find("<|im_end|>") != std::string::npos) break;

        // Prepare next batch
        common_batch_clear(batch);
        common_batch_add(batch, new_token, n_cur, {0}, true);
        n_cur++;

        if (llama_decode(ctx, batch) != 0) {
            std::cerr << "[LLM] Decode failed during generation" << std::endl;
            break;
        }
    }

    llama_sampler_free(sampler);
    llama_batch_free(batch);
    llama_free(ctx);

    // Remove <|im_end|> if present
    auto pos = output.find("<|im_end|>");
    if (pos != std::string::npos) output = output.substr(0, pos);

    std::cout << "[LLM] Raw output: " << output << std::endl;
    return parse_output(output);
}

SentimentResult InferenceEngine::parse_output(const std::string& raw_output) {
    // Try to find JSON in the output
    auto start = raw_output.find('{');
    auto end = raw_output.rfind('}');
    if (start == std::string::npos || end == std::string::npos || end <= start) {
        return {"neutral", 0.0f, {}};
    }

    std::string json_str = raw_output.substr(start, end - start + 1);
    try {
        auto j = json::parse(json_str);
        SentimentResult result;
        result.sentiment = j.value("sentiment", "neutral");
        result.confidence = j.value("confidence", 0.5f);
        if (j.contains("keywords") && j["keywords"].is_array()) {
            for (auto& kw : j["keywords"]) {
                if (kw.is_string()) result.keywords.push_back(kw.get<std::string>());
            }
        }
        // Validate sentiment value
        if (result.sentiment != "positive" && result.sentiment != "negative" && result.sentiment != "neutral") {
            result.sentiment = "neutral";
        }
        return result;
    } catch (const std::exception& e) {
        std::cerr << "[LLM] JSON parse error: " << e.what() << std::endl;
        return {"neutral", 0.0f, {}};
    }
}

bool InferenceEngine::cache_get(const std::string& token, SentimentResult& out) {
    std::lock_guard<std::mutex> lock(cache_mutex_);
    auto it = cache_map_.find(token);
    if (it == cache_map_.end()) return false;
    // Move to front (most recently used)
    cache_list_.splice(cache_list_.begin(), cache_list_, it->second);
    out = it->second->second;
    return true;
}

void InferenceEngine::cache_put(const std::string& token, const SentimentResult& result) {
    std::lock_guard<std::mutex> lock(cache_mutex_);
    auto it = cache_map_.find(token);
    if (it != cache_map_.end()) {
        it->second->second = result;
        cache_list_.splice(cache_list_.begin(), cache_list_, it->second);
        return;
    }
    cache_list_.emplace_front(token, result);
    cache_map_[token] = cache_list_.begin();
    if (cache_map_.size() > MAX_CACHE_SIZE) {
        auto& back = cache_list_.back();
        cache_map_.erase(back.first);
        cache_list_.pop_back();
    }
}
