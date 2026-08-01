\set ON_ERROR_STOP on
BEGIN;

DO $$
DECLARE
  v_creator uuid;
  v_worker uuid;
  v_social uuid;
  v_task uuid;
  v_bounty uuid;
  v_claim uuid;
  v_submission uuid;
  v_auth uuid;
  v_payment uuid;
  v_eligible integer;
BEGIN
  INSERT INTO users (
    email, display_name, can_create_tasks, prava_account_ref, prava_account_status
  ) VALUES (
    'creator@example.com', 'Campaign Creator', true, 'prava_creator_test', 'active'
  ) RETURNING id INTO v_creator;

  INSERT INTO users (
    email, display_name, can_work_tasks, bio,
    prava_account_ref, prava_account_status
  ) VALUES (
    'worker@example.com', 'Reddit Freelancer', true, 'Marketing specialist',
    'prava_worker_test', 'active'
  ) RETURNING id INTO v_worker;

  INSERT INTO social_accounts (
    user_id, platform, handle, profile_url, follower_count, following_count,
    reddit_post_karma, reddit_comment_karma, is_verified, verified_at,
    enrichment_provider, enriched_at
  ) VALUES (
    v_worker, 'reddit', 'worker_handle', 'https://reddit.com/u/worker_handle',
    2500, 200, 4000, 6000, true, now(), 'parallel', now()
  ) RETURNING id INTO v_social;

  INSERT INTO tasks (
    creator_id, title, description, total_budget_minor, currency, status
  ) VALUES (
    v_creator, 'Launch campaign', 'Promote a launch with authentic comments.',
    10000, 'USD', 'open'
  ) RETURNING id INTO v_task;

  INSERT INTO bounties (
    task_id, platform, action, title, instructions, reward_minor, slots_total,
    influence_metric, min_influence, max_influence, proof_requirements, status
  ) VALUES (
    v_task, 'reddit', 'comment', 'Comment on a Reddit thread',
    'Write a relevant comment and submit its URL plus screenshot.',
    5000, 2, 'karma', 5000, 20000, '["url", "screenshot"]', 'open'
  ) RETURNING id INTO v_bounty;

  SELECT count(*) INTO v_eligible FROM get_eligible_bounties(v_worker)
   WHERE bounty_id = v_bounty AND open_slots = 2;
  IF v_eligible <> 1 THEN
    RAISE EXCEPTION 'freelancer eligibility feed failed';
  END IF;

  SELECT id INTO v_claim
    FROM claim_bounty(v_bounty, v_worker, v_social, interval '24 hours');

  INSERT INTO submissions (claim_id, note)
  VALUES (v_claim, 'Completed as instructed')
  RETURNING id INTO v_submission;

  INSERT INTO submission_proofs (submission_id, kind, external_url)
  VALUES (v_submission, 'url', 'https://reddit.com/r/example/comments/abc/comment/xyz');

  -- Passing verification without every creator-required proof must fail.
  BEGIN
    UPDATE submissions
       SET verification_method = 'automatic', verification_status = 'passed', verified_at = now()
     WHERE id = v_submission;
    RAISE EXCEPTION 'required-proof enforcement failed';
  EXCEPTION WHEN raise_exception THEN
    IF SQLERRM = 'required-proof enforcement failed' THEN RAISE; END IF;
  END;

  INSERT INTO submission_proofs (submission_id, kind, storage_key, mime_type, sha256)
  VALUES (v_submission, 'screenshot', 'proofs/test/screenshot.png', 'image/png', repeat('a', 64));

  UPDATE submissions
     SET verification_method = 'automatic',
         verification_status = 'passed',
         verification_checks = '{"url_reachable":true,"platform_matches":true}',
         verified_at = now()
   WHERE id = v_submission;

  IF (SELECT status FROM bounty_claims WHERE id = v_claim) <> 'approved' THEN
    RAISE EXCEPTION 'verification did not approve claim';
  END IF;

  INSERT INTO payment_authorizations (
    task_id, creator_id, provider_authorization_ref, status,
    per_payment_cap_minor, total_cap_minor, max_payments, currency
  ) VALUES (
    v_task, v_creator, 'prava_auth_test', 'active', 5000, 10000, 2, 'USD'
  ) RETURNING id INTO v_auth;

  INSERT INTO payments (
    authorization_id, task_id, bounty_id, claim_id, submission_id,
    payer_user_id, payee_user_id, amount_minor, currency, idempotency_key
  ) VALUES (
    v_auth, v_task, v_bounty, v_claim, v_submission,
    v_creator, v_worker, 5000, 'USD', 'payout:test:claim-1'
  ) RETURNING id INTO v_payment;

  INSERT INTO payment_attempts (
    payment_id, attempt_number, provider_session_ref, provider_transaction_ref,
    status, request_data, response_data, completed_at
  ) VALUES (
    v_payment, 1, 'prava_session_test', 'prava_tx_test', 'succeeded',
    '{"amount_minor":5000,"currency":"USD"}', '{"accepted":true}', now()
  );

  UPDATE payments
     SET status = 'succeeded', provider_transaction_ref = 'prava_tx_test', completed_at = now()
   WHERE id = v_payment;

  IF (SELECT status FROM bounty_claims WHERE id = v_claim) <> 'paid' THEN
    RAISE EXCEPTION 'successful payout did not mark claim paid';
  END IF;
  IF (SELECT used_minor FROM payment_authorizations WHERE id = v_auth) <> 5000 THEN
    RAISE EXCEPTION 'authorization usage was not recorded';
  END IF;

  -- Budget enforcement must reject an over-allocation.
  BEGIN
    INSERT INTO bounties (
      task_id, platform, action, title, instructions, reward_minor, slots_total,
      influence_metric, proof_requirements, status
    ) VALUES (
      v_task, 'linkedin', 'post', 'Over budget', 'This must fail.',
      1, 1, 'followers', '["url"]', 'draft'
    );
    RAISE EXCEPTION 'budget enforcement failed';
  EXCEPTION WHEN raise_exception THEN
    IF SQLERRM = 'budget enforcement failed' THEN RAISE; END IF;
  END;

  INSERT INTO api_clients (creator_id, name, client_key, secret_hash, scopes)
  VALUES (v_creator, 'Creator agent', 'client_test', 'argon2id:test', ARRAY['tasks:write']);

  INSERT INTO mcp_requests (
    api_client_id, method, idempotency_key, status, request_data, response_data, completed_at
  ) SELECT id, 'create_task', 'mcp:test:1', 'succeeded',
           '{"title":"Launch campaign"}', jsonb_build_object('task_id', v_task), now()
      FROM api_clients WHERE client_key = 'client_test';

  INSERT INTO webhook_endpoints (creator_id, url, secret_hash, subscribed_events)
  VALUES (
    v_creator, 'https://example.test/webhooks/hire-a-human', 'hmac:test',
    ARRAY['submission.approved', 'payment.succeeded']
  );

  INSERT INTO webhook_deliveries (
    endpoint_id, event_type, entity_type, entity_id, payload
  ) SELECT id, 'payment.succeeded', 'payment', v_payment,
           jsonb_build_object('payment_id', v_payment, 'status', 'succeeded')
      FROM webhook_endpoints WHERE creator_id = v_creator;
END;
$$;

-- Guard against accidental storage of raw card secrets.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = 'users'
       AND column_name IN ('status', 'trust_score')
  ) THEN
    RAISE EXCEPTION 'unwanted user status/trust_score column found';
  END IF;
  IF (
    SELECT count(*) FROM information_schema.columns
     WHERE table_schema = 'public'
       AND column_name = 'deadline_at'
       AND table_name IN ('tasks', 'bounties')
  ) <> 2 THEN
    RAISE EXCEPTION 'task or bounty deadline column is missing';
  END IF;
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_schema = 'public'
       AND lower(column_name) IN (
         'card_number', 'pan', 'cvv', 'cvc', 'security_code', 'expiry', 'expiry_date'
       )
  ) THEN
    RAISE EXCEPTION 'sensitive card column found';
  END IF;
END;
$$;

ROLLBACK;
SELECT 'all smoke tests passed' AS result;
