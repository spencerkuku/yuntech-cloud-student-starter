# W5 personal implementation

This directory follows the W4 layout and contains Spencer's W5 implementation:

- `app/service.py`: PostgreSQL-backed event service with idempotent writes.
- `deploy/db-up.sh`: creates the private RDS network and PostgreSQL instance.
- `deploy/deploy.sh`: deploys a committed service and the local secrets.
- `deploy/make_user_data.py`: packages the committed W5 service for EC2.
- `tests/test_w05_service.py`: offline service tests using the in-memory fallback.
- `tests/run_w05_matrix.py`: the five-row live idempotency matrix.

Before any AWS write, copy `w5.env.example` to `.local/w5.env`, verify the
recorded W3 instance, and review the resource approval printed by `db-up.sh`.
Keep `.local/` out of Git and never include secrets in reports.
