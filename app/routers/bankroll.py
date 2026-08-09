from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime

from ..database import get_db
from .. import models, schemas

router = APIRouter(prefix="/bankroll", tags=["bankroll"])


@router.get("/")
def get_bankroll(db: Session = Depends(get_db)):
    state = db.query(models.BankrollState).get(1)
    if not state:
        return {"initialized": False, "current_balance": None, "initial_balance": None}
    return {
        "initialized": True,
        "current_balance": state.current_balance,
        "initial_balance": state.initial_balance,
        "updated_at": state.updated_at,
    }


@router.post("/set")
def set_bankroll(req: schemas.BankrollSet, db: Session = Depends(get_db)):
    """証拠金の初期設定、またはリセット(入金・出金の反映)に使う。"""
    state = db.query(models.BankrollState).get(1)
    if state:
        state.current_balance = req.initial_balance
        state.initial_balance = req.initial_balance
        state.updated_at = datetime.utcnow()
    else:
        state = models.BankrollState(
            id=1, current_balance=req.initial_balance, initial_balance=req.initial_balance
        )
        db.add(state)
    db.commit()
    db.refresh(state)
    return {
        "current_balance": state.current_balance,
        "initial_balance": state.initial_balance,
    }


def get_current_balance(db: Session) -> float:
    state = db.query(models.BankrollState).get(1)
    if not state:
        raise HTTPException(
            400,
            "証拠金がまだ設定されていません。先に「証拠金を設定」から初期額を登録してください"
        )
    return state.current_balance


def adjust_balance(db: Session, delta: float):
    """購入時(マイナス)・払戻時(プラス)に残高を増減する。"""
    state = db.query(models.BankrollState).get(1)
    if not state:
        return
    state.current_balance += delta
    state.updated_at = datetime.utcnow()
    db.commit()
