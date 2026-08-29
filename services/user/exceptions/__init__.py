from fastapi import HTTPException, status


class UserErrors:
    @staticmethod
    def username_taken() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Username is already taken"
        )

    @staticmethod
    def not_found() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )

    @staticmethod
    def no_user_specified() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No user specified"
        )
