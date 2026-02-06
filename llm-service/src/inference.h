#pragma once
#include <string>
#include <unordered_map>
#include <list>
#include <mutex>
#include "json.hpp"

using json = nlohmann::json;

struct SentimentResult {
    std::string sentiment;  // "positive", "negative", "neutral"
    float confidence;
    std::vector<std::string> keywords;

    json to_json() const {
        return json{
            {"sentiment", sentiment},
            {"confidence", confidence},
            {"keywords", keywords}
        };
    }
};

class InferenceEngine {
public:
    InferenceEngine();
    ~InferenceEngine();

    // Load the GGUF model from path. Returns false on failure.
    bool load_model(const std::string& model_path, int n_ctx = 2048, int n_threads = 2);

    // Run sentiment analysis on a Hebrew description.
    // Returns a SentimentResult. Thread-safe (uses internal mutex).
    SentimentResult analyze(const std::string& description);

    // Check if model is loaded
    bool is_loaded() const { return model_ != nullptr; }

    // LRU cache lookup by token
    bool cache_get(const std::string& token, SentimentResult& out);
    void cache_put(const std::string& token, const SentimentResult& result);

private:
    SentimentResult parse_output(const std::string& raw_output);

    struct llama_model* model_ = nullptr;
    int n_ctx_ = 2048;
    int n_threads_ = 2;

    // LRU cache
    static constexpr size_t MAX_CACHE_SIZE = 1000;
    std::mutex cache_mutex_;
    std::list<std::pair<std::string, SentimentResult>> cache_list_;
    std::unordered_map<std::string, decltype(cache_list_)::iterator> cache_map_;

    // Inference mutex — llama.cpp context is not thread-safe
    std::mutex inference_mutex_;
};
