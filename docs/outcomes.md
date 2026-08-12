# Outcomes

Outcomes record whether useful work happened. API success is not automatically an outcome success. Records may include success, score, threshold, quantity, unit, evaluator, evidence reference, correction status, confidence, and metadata.

An explicit `success: false` is a known failed outcome. A record without an explicit boolean success value and without both score and threshold has an unknown outcome. Invalidated outcomes count as known non-successes. Unknown outcomes are never silently counted as failures in success rate or cost-per-success calculations.
