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
        return {"initialized": False, "current_balance": None, "initial_balance": None, "race_cap_pct": None}
    return {
        "initialized": True,
        "current_balance": state.current_balance,
        "initial_balance": state.initial_balance,
        "updated_at": state.updated_at,
        "race_cap_pct": state.race_cap_pct,
    }


@router.post("/set-race-cap")
def set_race_cap(req: schemas.RaceCapSet, db: Session = Depends(get_db)):
    """
    資金管理シミュレーション(破産確率)で確認した、1レースあたりの安全な上限比率を保存する。
    以前は画面の手入力欄(既定100%=証拠金全額)とパイプラインの固定値(10%)がズレており、
    実運用として危険だった。この値が両方の唯一の情報源になる
    (のんの指摘により2026-09-06に追加)。
    """
    if not (0 < req.race_cap_pct <= 1):
        raise HTTPException(400, "race_cap_pctは0より大きく1以下(0.10=10%)で指定してください")
    state = db.query(models.BankrollState).get(1)
    if not state:
        raise HTTPException(400, "証拠金がまだ設定されていません。先に「証拠金を設定」から初期額を登録してください")
    state.race_cap_pct = req.race_cap_pct
    state.updated_at = datetime.utcnow()
    db.commit()
    return {"race_cap_pct": state.race_cap_pct}


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


def get_race_cap_pct(db: Session) -> float:
    """
    1レースあたりの上限比率。資金管理シミュレーションで確認した値を使う。
    未設定(証拠金自体が未設定)の場合は安全側のデフォルト10%を返す。
    """
    state = db.query(models.BankrollState).get(1)
    if not state or state.race_cap_pct is None:
        return 0.10
    return state.race_cap_pct


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
