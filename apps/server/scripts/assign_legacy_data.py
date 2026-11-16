from __future__ import annotations

import argparse

from app.domain.models import AgentPromptPreset, User, Workspace
from app.infrastructure.database import SessionLocal


def main() -> None:
    parser = argparse.ArgumentParser(description="Bind legacy unowned workspaces and custom prompts to an existing account.")
    parser.add_argument("--email", required=True, help="Email of the account that should own existing data")
    args = parser.parse_args()
    email = args.email.strip().lower()

    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise SystemExit(f"No account found for {email!r}; register first, then rerun this command.")
        workspaces = db.query(Workspace).filter(Workspace.owner_user_id.is_(None)).update(
            {Workspace.owner_user_id: user.id}, synchronize_session=False
        )
        prompts = db.query(AgentPromptPreset).filter(AgentPromptPreset.owner_user_id.is_(None)).update(
            {AgentPromptPreset.owner_user_id: user.id}, synchronize_session=False
        )
        db.commit()
        print(f"Assigned {workspaces} workspace(s) and {prompts} prompt preset(s) to {email}.")


if __name__ == "__main__":
    main()
