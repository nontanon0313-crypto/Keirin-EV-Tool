from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from ..database import get_db
from .. import models, schemas

router = APIRouter(prefix="/bank", tags=["bank"])


@router.get("/", response_model=List[schemas.BankMasterOut])
def list_banks(db: Session = Depends(get_db)):
    return db.query(models.BankMaster).all()


@router.post("/", response_model=schemas.BankMasterOut)
def create_bank(bank: schemas.BankMasterCreate, db: Session = Depends(get_db)):
    existing = db.query(models.BankMaster).filter(models.BankMaster.name == bank.name).first()
    if existing:
        raise HTTPException(400, "同名のバンクが既に登録されています")
    obj = models.BankMaster(**bank.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.put("/{bank_id}", response_model=schemas.BankMasterOut)
def update_bank(bank_id: int, bank: schemas.BankMasterCreate, db: Session = Depends(get_db)):
    obj = db.query(models.BankMaster).get(bank_id)
    if not obj:
        raise HTTPException(404, "バンクが見つかりません")
    for k, v in bank.model_dump().items():
        setattr(obj, k, v)
    db.commit()
    db.refresh(obj)
    return obj
