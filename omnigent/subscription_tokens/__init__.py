"""Subscription-aware token management.

Rotates across multiple provider subscriptions (and API-key tier fallbacks),
tracking each account's usage-limit windows, reset timers, and cost so a launch
routes to an account with headroom instead of hitting a rate limit.
"""
