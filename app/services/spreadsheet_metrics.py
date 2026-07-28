from __future__ import annotations

from typing import Any

COMPUTED_COLUMNS = (
    "E", "F", "G", "I", "J", "K", "L", "S", "Z", "AG", "AH", "AI", "AJ",
    "AK", "AL", "AM", "AN", "AO", "AP", "AQ", "AR", "AS", "AT", "AU",
    "AV", "AW", "AX", "AY", "AZ", "BA", "BD", "BE", "BF", "BG", "BH",
    "BI", "BJ", "BK", "BL", "BM", "BP", "BU", "BV", "BY", "BZ", "CA",
    "CB", "CC", "CD", "CO", "CP", "CQ", "CS", "CT", "CV", "CW", "DA",
    "DG", "DH", "DP", "DS", "DV", "DY", "EF", "EG", "EH", "EI",
)

INVALID_FORMULA_VALUES = {"", "#VALUE!", "#DIV/0!", "#N/A", "#REF!", "#NAME?", "#NUM!", "#NULL!"}


def _n(data: dict[str, Any], key: str) -> float:
    value = data.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if value in (None, ""):
        return 0.0
    try:
        return float(str(value).replace(",", ".").strip())
    except ValueError:
        return 0.0


def _has_source_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().upper() not in INVALID_FORMULA_VALUES
    return True


def _div(a: float, b: float) -> float:
    return a / b if b else 0.0


def _yes(condition: bool, weight: float = 1.0) -> float:
    return weight if condition else 0.0


def calculate_signal_columns(data: dict[str, Any]) -> None:
    """Вычисляет необходимые для бота формульные столбцы.

    В исходном XLSX формулы не имеют сохранённого кэша, поэтому openpyxl
    видит в data_only режиме None. Расчёты ниже повторяют формулы таблицы и
    записывают результаты под буквенными ключами столбцов. Если таблица уже вернула
    готовое значение формульной колонки, оно остается источником истины.
    """
    source_values = {key: data[key] for key in COMPUTED_COLUMNS if _has_source_value(data.get(key))}

    M,N,O,P = (_n(data, c) for c in ("M","N","O","P"))
    Q,R = (_n(data, c) for c in ("Q","R"))
    T,U,V,W = (_n(data, c) for c in ("T","U","V","W"))
    X,Y = (_n(data, c) for c in ("X","Y"))
    AA,AB,AC,AD = (_n(data, c) for c in ("AA","AB","AC","AD"))
    AE,AF = (_n(data, c) for c in ("AE","AF"))
    CM,CN = (_n(data, c) for c in ("CM","CN"))
    DL,DM,DN,DO = (_n(data, c) for c in ("DL","DM","DN","DO"))
    DQ,DR,DT,DU,DW,DX = (_n(data, c) for c in ("DQ","DR","DT","DU","DW","DX"))

    S = _div(Q, Q+R)
    Z = _div(X, X+Y)
    AG = _div(AE, (AA+AB)/100)
    AH = _div(AF, (AA+AB)/100)
    CO = CN-CM
    CP = AA+AB
    AI = _div(CO, CP)
    AJ = _div(Z, S+Z)
    AK = _div(AA, AB+AA) + _div(AC, AD+AC)
    AL = _div(AB, AA+AB) + _div(AD, AC+AD)
    K = _div(M, N+M) + _div(O, P+O)
    L = _div(T, U+T) + _div(V, W+V)
    AM = _div(AK, AK+K)
    AN = _div(AL, AL+L)
    I = AM-AN
    AO = AM-AN
    AP = (AC-(AA*3))-AB
    AQ = (O-(M*3))-N
    AR = (AD-(AB*3))-AA
    AS = (V-(T*3))-U
    AT = _div(O-(M*3), N)
    AU = _div(V-(T*3), U)
    AV = _div(AC, AC+AD)
    AW = _div(AD, AC+AD)
    AX = 0.4*K + 0.3*AP + 0.2*I + 0.1*AT
    AY = 0.4*L + 0.3*AR + (-0.2)*I + 0.1*AU
    J = (AX-AY)*0.1
    AZ = _div(O, (M+N)*3)
    BA = _div(V, (T+U)*3)
    BB = _div(AC, (AA+AB)*3)
    BC = _div(AD, (AA+AB)*3)
    BD = (AZ+BB)/2
    BE = (BA+BC)/2
    BF = _div(AC, AB+AA)
    BG = _div(AD, AA+AB)
    BH = _div(O, M+N)
    BI = _div(V, T+U)
    BJ = _div(O, P)
    BK = _div(AC, AD)
    BL = _div(V, W)
    BM = _div(AD, AC)
    BN = BJ-BL
    BO = BK-BM
    BP = (BN+BO)/2
    BU = _div(O, O+P)
    BV = _div(V, V+W)
    BY = BH-BI
    BZ = BI-BH
    CA = AA-AB
    CB = AB-AA
    CC = _div(AC, AA+AB)-_div(AD, AA+AB)
    CD = _div(AD, AA+AB)-_div(AC, AA+AB)
    CQ = _div(CO, AC+AD)
    DA = AV-AW
    G = ((_div(O,P)+_div(AC,AD))/2)-((_div(V,W)+_div(AD,AC))/2)
    DP = ((DL-DN)-(DM-DO))/12
    DS = (DQ-DR)/5
    DV = (DT-DU)/5
    DY = (DW-DX)/5
    EF = (DS+DV+DY)/3

    E = sum((
        _yes(I>0.15,0.05), _yes(DA>0.15,0.03), _yes(J>0.11,0.05),
        _yes(CC>0,0.05), _yes(AP>0,0.03), _yes(AQ>AS,0.03),
        _yes(AR<AP,0.03), _yes(AS<0,0.03), _yes(BY>0.1,0.05),
        _yes(AX-AY>0.8,0.03), _yes(K>1,0.05), _yes(L<1,0.03),
        _yes(CA>1,0.03), _yes(BA<0.66,0.03), _yes(AO>0,0.03),
        _yes(AT>1,0.03), _yes(AU<1,0.03), _yes(BP>0,0.03),
        _yes(AG<7.1,0.05), _yes(BD>BE,0.03), _yes(AJ<0.5,0.03),
        _yes(BU>0.5,0.05), _yes(AI<-2.9,0.03), _yes(CQ<-0.2,0.03),
        _yes(AV>0.65,0.03), _yes(EF>2,0.05), _yes(DP>1,0.03),
        _yes(G>0.15,0.03),
    ))
    F = sum((
        _yes(I<-0.15,0.05), _yes(DA<-0.15,0.03), _yes(J<-0.11,0.05),
        _yes(CD>0,0.05), _yes(AR>0,0.03), _yes(AS>AQ,0.03),
        _yes(AP<AR,0.03), _yes(AQ<0,0.03), _yes(BZ>0.1,0.05),
        _yes(AY-AX>0.8,0.03), _yes(L>1,0.05), _yes(K<1,0.03),
        _yes(CB>1,0.03), _yes(AZ<0.66,0.03), _yes(AO<0,0.03),
        _yes(AU>1,0.03), _yes(AT<1,0.03), _yes(BP<0,0.03),
        _yes(AH<7.1,0.05), _yes(BE>BD,0.03), _yes(AJ>0.5,0.03),
        _yes(BV>0.5,0.05), _yes(AI>2.9,0.03), _yes(CQ>0.2,0.03),
        _yes(AW>0.65,0.03), _yes(EF<-2,0.05), _yes(DP<-1,0.03),
        _yes(G<-0.15,0.03),
    ))

    CS = sum((
        _yes(EF>2.2), _yes(DS>0), _yes(CP>4), _yes(E>0.7),
        _yes(DP>0.7), _yes(BG>1.25), _yes(G>0.6), _yes(BY if False else (BH-BI)>0.15),
        _yes(J>0.11), _yes(DA>0.15),
    ))
    CT = sum((
        _yes(EF<-2.2), _yes(DS<0), _yes(CP>4), _yes(F>0.7),
        _yes(DP<-0.7), _yes(BF>1.25), _yes(G<-0.6), _yes((BH-BI)<-0.15),
        _yes(J<-0.11), _yes(DA<-0.15),
    ))

    CV = E*100
    CW = F*100
    DG = sum((
        _yes(I>0.1), _yes(BA<0.66), _yes(DP>0.5), _yes(AG<7.5),
        _yes(E>0.6), _yes(AI<-2), _yes(AK>1), _yes(EF>1),
        _yes(AP>0), _yes(AX-AY>0.8),
    ))
    DH = sum((
        _yes(I<-0.1), _yes(AZ<0.66), _yes(DP<-0.5), _yes(AH<7.5),
        _yes(F>0.6), _yes(AI>2), _yes(AR>0), _yes(EF<-1),
        _yes(AL>1), _yes(AY-AX>0.8),
    ))
    EG = sum((_yes(EF>2), _yes(CP>4), _yes(E>0.65), _yes(DP>0.7), _yes(Q>6)))
    EH = sum((_yes(EF<-2), _yes(CP>4), _yes(F>0.65), _yes(DP<-0.7), _yes(X>6)))

    data.update({
        "E": E, "F": F, "G": G, "I": I, "J": J, "K": K, "L": L,
        "S": S, "Z": Z, "AG": AG, "AH": AH, "AI": AI, "AJ": AJ,
        "AK": AK, "AL": AL, "AM": AM, "AN": AN, "AO": AO,
        "AP": AP, "AQ": AQ, "AR": AR, "AS": AS, "AT": AT, "AU": AU,
        "AV": AV, "AW": AW, "AX": AX, "AY": AY, "AZ": AZ, "BA": BA,
        "BD": BD, "BE": BE, "BF": BF, "BG": BG, "BH": BH, "BI": BI,
        "BJ": BJ, "BK": BK, "BL": BL, "BM": BM, "BP": BP, "BU": BU,
        "BV": BV, "BY": BY, "BZ": BZ, "CA": CA, "CB": CB, "CC": CC,
        "CD": CD, "CO": CO, "CP": CP, "CQ": CQ, "DA": DA, "DP": DP,
        "DS": DS, "DV": DV, "DY": DY, "EF": EF, "DG": DG, "DH": DH, "CS": CS, "CT": CT,
        "CV": CV, "CW": CW, "EG": EG, "EH": EH, "EI": EG-EH,
    })
    data.update(source_values)
