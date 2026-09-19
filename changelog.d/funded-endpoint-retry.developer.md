Funded OpenRouter requests exclude endpoints that fail with rate-limit/server
errors from subsequent quotes in the same run. Retries retain unknown charges,
respect existing routing restrictions, and reserve the newly quoted endpoint
before dispatch. No automatic retries or provider-side fallback are added.
