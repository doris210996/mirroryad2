#pragma once
#include <string>

// Qwen2.5 chat template for Hebrew sentiment analysis
// The model should return valid JSON with a "sentiment" field

inline std::string build_sentiment_prompt(const std::string& description) {
    // Qwen2.5 uses ChatML format: <|im_start|>role\ncontent<|im_end|>
    std::string prompt;
    prompt += "<|im_start|>system\n";
    prompt += "Analyze real estate descriptions and return sentiment + key features. "
              "Reply with ONLY valid JSON, no other text:\n"
              "{\"sentiment\": \"positive\" | \"negative\" | \"neutral\", "
              "\"confidence\": 0.0-1.0, "
              "\"keywords\": [\"word1\", \"word2\", \"word3\"]}\n"
              "For sentiment: positive means the property is described favorably (spacious, renovated, quiet, good location). "
              "Negative means unfavorable (small, noisy, needs work). Neutral is factual/no clear opinion. "
              "For keywords: list 2-4 simple English words describing key features like: spacious, renovated, quiet, central, bright, modern.\n";
    prompt += "<|im_end|>\n";
    prompt += "<|im_start|>user\n";
    prompt += description;
    prompt += "\n<|im_end|>\n";
    prompt += "<|im_start|>assistant\n";
    return prompt;
}
