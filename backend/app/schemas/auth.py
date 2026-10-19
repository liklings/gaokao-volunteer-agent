from pydantic import BaseModel, EmailStr, field_validator


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str

    @field_validator("password")
    @classmethod
    def _password_min_length(cls, v: str) -> str:
        # 只做最基本的长度校验，不在后端强加"必须有大小写+数字+符号"这类规则——
        # 那类规则实际安全收益有限，还容易把用户逼去用"Aa1!Aa1!"这种反而更好猜的密码。
        if len(v) < 8:
            raise ValueError("密码长度至少需要8位")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: int
    email: str

    class Config:
        from_attributes = True


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut
