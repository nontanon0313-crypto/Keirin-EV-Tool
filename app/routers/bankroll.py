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
    """
    購入時(マイナス)・払戻時(プラス)に残高を増減する。

    以前は「Pythonで読み込んで加算し、書き戻す」形だったため、複数レースを
    同時に処理すると更新が競合し、一部の増減が失われる可能性があった
    (再予想の並列実行に対応するため、のんの要望により修正)。
    DB側で「current_balance = current_balance + delta」という原子的な更新に
    することで、同時に複数のリクエストが来ても正しく積み上がるようにした。
    """
    from sqlalchemy import update
    result = db.execute(
        update(models.BankrollState)
        .where(models.BankrollState.id == 1)
        .values(current_balance=models.BankrollState.current_balance + delta, updated_at=datetime.utcnow())
    )
    db.commit()
    return result.rowcount > 0
