import sys
import os

# Add the project root to the Python path to allow imports from chessdotcom_ai_coach
sys.path.append(os.getcwd())

from sqlmodel import Session, select
from chessdotcom_ai_coach.dependencies import engine
from chessdotcom_ai_coach.user import User
from chessdotcom_ai_coach.auth_service import get_password_hash


def create_user(username, password):
    """
    Creates a new user in the database with a hashed password.
    """
    with Session(engine) as session:
        # Check if user already exists
        statement = select(User).where(User.username == username)
        existing_user = session.exec(statement).first()

        if existing_user:
            print(f"Error: User '{username}' already exists.")
            return

        # Create the new user with hashed password
        hashed_pw = get_password_hash(password)
        new_user = User(username=username, hashed_password=hashed_pw)

        try:
            session.add(new_user)
            session.commit()
            print(f"User '{username}' created successfully!")
        except Exception as e:
            session.rollback()
            print(f"An error occurred while creating the user: {e}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/create_user.py <username> <password>")
    else:
        user = sys.argv[1]
        pw = sys.argv[2]
        create_user(user, pw)
