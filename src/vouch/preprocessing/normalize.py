"""
Normalizes raw input user message for handling unicode trickeries and a stable cache representation

1. NFKC Normalization
2. Strip zero-width characters
3. Homoglyph / confusable folding
4. Entity extraction using regex (phone numbers, upi id, etc.)
5. Store in cache
"""
