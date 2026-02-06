#include <iostream>
#include <string>
#include <cstdlib>

#define CPPHTTPLIB_OPENSSL_SUPPORT 0
#include "httplib.h"
#include "json.hpp"
#include "inference.h"

using json = nlohmann::json;

int main(int argc, char* argv[]) {
    std::string model_path = "models/qwen2.5-1.5b-instruct-q4_k_m.gguf";
    int port = 8282;
    int n_ctx = 2048;
    int n_threads = 2;

    // Parse command-line args
    for (int i = 1; i < argc; i++) {
        std::string arg = argv[i];
        if (arg == "--model" && i + 1 < argc) model_path = argv[++i];
        else if (arg == "--port" && i + 1 < argc) port = std::atoi(argv[++i]);
        else if (arg == "--ctx" && i + 1 < argc) n_ctx = std::atoi(argv[++i]);
        else if (arg == "--threads" && i + 1 < argc) n_threads = std::atoi(argv[++i]);
    }

    // Initialize inference engine
    InferenceEngine engine;
    if (!engine.load_model(model_path, n_ctx, n_threads)) {
        std::cerr << "Failed to load model from: " << model_path << std::endl;
        return 1;
    }

    // HTTP server
    httplib::Server svr;

    // CORS headers for Flask integration
    svr.set_default_headers({
        {"Access-Control-Allow-Origin", "*"},
        {"Access-Control-Allow-Methods", "GET, POST, OPTIONS"},
        {"Access-Control-Allow-Headers", "Content-Type"},
    });

    // Health check
    svr.Get("/health", [&](const httplib::Request&, httplib::Response& res) {
        json resp = {{"status", "ok"}, {"model_loaded", engine.is_loaded()}};
        res.set_content(resp.dump(), "application/json");
    });

    // OPTIONS for CORS preflight
    svr.Options("/analyze", [](const httplib::Request&, httplib::Response& res) {
        res.status = 204;
    });

    // Analyze endpoint
    svr.Post("/analyze", [&](const httplib::Request& req, httplib::Response& res) {
        try {
            auto body = json::parse(req.body);
            if (!body.contains("items") || !body["items"].is_array()) {
                res.status = 400;
                res.set_content(json{{"error", "Missing 'items' array"}}.dump(), "application/json");
                return;
            }

            json results = json::object();
            for (auto& item : body["items"]) {
                std::string token = item.value("token", "");
                std::string description = item.value("description", "");

                if (token.empty() || description.empty()) continue;

                // Check cache first
                SentimentResult cached;
                if (engine.cache_get(token, cached)) {
                    results[token] = cached.to_json();
                    continue;
                }

                // Run inference
                auto result = engine.analyze(description);
                engine.cache_put(token, result);
                results[token] = result.to_json();
            }

            json resp = {{"results", results}};
            res.set_content(resp.dump(), "application/json");
        } catch (const std::exception& e) {
            res.status = 500;
            res.set_content(json{{"error", e.what()}}.dump(), "application/json");
        }
    });

    std::cout << "[LLM Service] Starting on port " << port << std::endl;
    svr.listen("0.0.0.0", port);

    return 0;
}
