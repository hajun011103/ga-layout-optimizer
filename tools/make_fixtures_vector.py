# -*- coding: utf-8 -*-
"""
make_fixtures_vector.py — 벡터·목록 가져오기 시험용 표본 파일 생성 (표준 라이브러리만, 결정론적)

  python3 tools/make_fixtures_vector.py        → tests/fixtures/ 에 기록

만드는 것 (치수는 tests/test_import_vector.py 의 기대값과 1:1 대응)
  plant_mm.dxf      $INSUNITS=4(mm). 경계·실 3·기기 INSERT 2(속성 TAG)·LINE 4개 직사각형·기둥·MTEXT(한글)
  plant_cp949.dxf   CP949 인코딩, $INSUNITS 없음 (units 옵션 필요)
  plant.svg         width/height mm + viewBox, 중첩 g transform, path 실, 한글 text
  plant.ifc         IFC4, 층 2, IfcSpace 2, IfcBuildingElementProxy 1 (90° 회전)
  equipment.csv     CP949, 머리글 단위 표기, 좌표 없음, 치수 누락 행 1
  equipment.xlsx    zipfile 로 직접 조립, 제목 행 + 머리글, 좌표 있음
"""
import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tests", "fixtures")


# ---------------------------------------------------------------------------
# DXF
# ---------------------------------------------------------------------------

class _Dxf:
    def __init__(self):
        self.o = []
        self.h = 0x100

    def g(self, c, v):
        self.o.append(str(c)); self.o.append(str(v))

    def handle(self):
        self.h += 1
        self.g(5, f"{self.h:X}")

    def lwpoly(self, layer, pts, closed=True):
        self.g(0, "LWPOLYLINE"); self.handle(); self.g(8, layer); self.g(100, "AcDbEntity")
        self.g(100, "AcDbPolyline"); self.g(90, len(pts)); self.g(70, 1 if closed else 0)
        for x, y in pts:
            self.g(10, x); self.g(20, y)

    def rect(self, layer, x0, y0, x1, y1):
        self.lwpoly(layer, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)])

    def line(self, layer, x0, y0, x1, y1):
        self.g(0, "LINE"); self.handle(); self.g(8, layer)
        self.g(10, x0); self.g(20, y0); self.g(30, 0); self.g(11, x1); self.g(21, y1); self.g(31, 0)

    def text(self, layer, x, y, s, h=250):
        self.g(0, "TEXT"); self.handle(); self.g(8, layer)
        self.g(10, x); self.g(20, y); self.g(30, 0); self.g(40, h); self.g(1, s)

    def mtext(self, layer, x, y, s, h=250):
        self.g(0, "MTEXT"); self.handle(); self.g(8, layer)
        self.g(10, x); self.g(20, y); self.g(30, 0); self.g(40, h); self.g(1, s)

    def insert(self, layer, block, x, y, rot=0.0, sx=1.0, sy=1.0, attribs=()):
        self.g(0, "INSERT"); self.handle(); self.g(8, layer)
        if attribs:
            self.g(66, 1)
        self.g(2, block); self.g(10, x); self.g(20, y); self.g(30, 0)
        if sx != 1.0:
            self.g(41, sx)
        if sy != 1.0:
            self.g(42, sy)
        if rot:
            self.g(50, rot)
        for tag, val in attribs:
            self.g(0, "ATTRIB"); self.handle(); self.g(8, layer)
            self.g(10, x); self.g(20, y); self.g(30, 0); self.g(40, 200)
            self.g(1, val); self.g(2, tag); self.g(70, 0)
        if attribs:
            self.g(0, "SEQEND"); self.handle(); self.g(8, layer)

    def text_out(self):
        return "\n".join(self.o) + "\n"


def make_dxf():
    d = _Dxf()
    g = d.g
    g(999, "smr-ga test fixture")
    g(0, "SECTION"); g(2, "HEADER")
    g(9, "$ACADVER"); g(1, "AC1018")
    g(9, "$INSUNITS"); g(70, 4)
    g(0, "ENDSEC")
    # BLOCKS
    g(0, "SECTION"); g(2, "BLOCKS")
    # PUMP: 2000×1000 mm, 기준점 (0,0)
    g(0, "BLOCK"); g(8, "0"); g(2, "PUMP"); g(70, 2); g(10, 0); g(20, 0); g(30, 0); g(3, "PUMP")
    d.rect("0", 0, 0, 2000, 1000)
    g(0, "ATTDEF"); g(8, "0"); g(10, 100); g(20, 100); g(40, 200); g(1, ""); g(3, "TAG?"); g(2, "TAG"); g(70, 0)
    g(0, "ENDBLK"); g(8, "0")
    # SKID: 3000×2000 mm 프레임 + 중첩 PUMP
    g(0, "BLOCK"); g(8, "0"); g(2, "SKID"); g(70, 0); g(10, 0); g(20, 0); g(30, 0); g(3, "SKID")
    d.rect("0", 0, 0, 3000, 2000)
    d.insert("0", "PUMP", 500, 500)
    g(0, "ENDBLK"); g(8, "0")
    g(0, "ENDSEC")
    # ENTITIES
    g(0, "SECTION"); g(2, "ENTITIES")
    d.rect("BOUNDARY", 0, 0, 30000, 20000)
    d.rect("ROOM", 1000, 1000, 11000, 9000)        # R1 10×8 m
    d.rect("ROOM", 12000, 1000, 20000, 9000)       # R2 8×8 m
    d.rect("ROOM", 1000, 10000, 15000, 19000)      # R3 14×9 m
    d.mtext("ROOM_TEXT", 2000, 2000, r"\A1;{\fGulim|b0|i0|c129|p49;펌프실}")
    d.text("ROOM_TEXT", 13000, 8000, r"\U+C804\U+AE30\U+C2E4")          # 전기실
    d.mtext("ROOM_TEXT", 2000, 18000, r"{\H1.2x;제어실}\PMCR-01")
    # 기기: INSERT 2
    d.insert("EQUIP", "PUMP", 3000, 3000,
             attribs=[("TAG", "P-101"), ("NAME", "냉각수 펌프 A")])
    d.insert("EQUIP", "SKID", 17000, 2000, rot=90.0, attribs=[("TAG", "P-102")])
    # 기기: LINE 4 개로 그린 3×2 m
    d.line("EQUIP", 21000, 2000, 24000, 2000)
    d.line("EQUIP", 24000, 2000, 24000, 4000)
    d.line("EQUIP", 24000, 4000, 21000, 4000)
    d.line("EQUIP", 21000, 4000, 21000, 2000)
    d.text("TEXT", 21500, 2500, "HX-301")
    # 기둥 0.5×0.5 m
    d.rect("COLUMN", 25000, 15000, 25500, 15500)
    # 무시되어야 하는 것
    d.line("DIM", 0, -1000, 30000, -1000)
    d.text("DIM", 21600, 3000, "3000")
    g(0, "ENDSEC"); g(0, "EOF")
    return d.text_out().encode("utf-8")


def make_dxf_cp949():
    d = _Dxf()
    g = d.g
    g(0, "SECTION"); g(2, "HEADER"); g(9, "$DWGCODEPAGE"); g(3, "ANSI_949"); g(0, "ENDSEC")
    g(0, "SECTION"); g(2, "ENTITIES")
    d.rect("2F_ROOM", 0, 0, 4000, 3000)
    d.text("2F_ROOM", 500, 500, "배터리실")
    g(0, "ENDSEC"); g(0, "EOF")
    return d.text_out().encode("cp949")


# ---------------------------------------------------------------------------
# SVG — 300 mm × 200 mm 종이, viewBox 3000×2000 → 1 사용자단위 = 0.1 mm. 1:100 → 0.01 m
# ---------------------------------------------------------------------------

SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
     width="300mm" height="200mm" viewBox="0 0 3000 2000">
  <defs><rect id="defs-rect-should-be-ignored" x="0" y="0" width="99" height="99"/></defs>
  <rect id="site-boundary" x="0" y="0" width="3000" height="2000" fill="none"/>
  <g id="layer1" inkscape:label="1F">
    <g id="rooms" transform="translate(100,100)">
      <path id="room-A" d="M0,0 H1000 V800 H0 Z"/>
      <path id="room-B" d="m1100,0 l800,0 0,800 -800,0z"/>
    </g>
    <g id="equip" transform="translate(200,200)">
      <rect id="P-201" x="0" y="0" width="200" height="100"/>
      <text x="50" y="50"><tspan>P-201</tspan></text>
      <text x="50" y="80">급수펌프</text>
      <g transform="translate(1500,200) rotate(90)">
        <rect class="pump" x="0" y="0" width="300" height="100"/>
      </g>
      <text x="1450" y="350">P-202</text>
    </g>
    <circle class="column" cx="1050" cy="1500" r="25"/>
    <text x="300" y="800">펌프실</text>
    <g id="tank-lines" class="equipment">
      <line x1="300" y1="1200" x2="600" y2="1200"/>
      <line x1="600" y1="1200" x2="600" y2="1400"/>
      <line x1="600" y1="1400" x2="300" y2="1400"/>
      <line x1="300" y1="1400" x2="300" y2="1200"/>
    </g>
    <text x="400" y="1300">TK-101</text>
    <g id="dim"><text x="2500" y="1900">치수 30000</text></g>
  </g>
  <g id="layer2" inkscape:label="2F" transform="matrix(1,0,0,1,0,0)">
    <path id="room-C" class="room" d="M 200 1200 C 200 1200 700 1200 700 1200 L7e2,1.6e3 L200,1600 Z"/>
    <text x="300" y="1500">제어실</text>
  </g>
</svg>
"""


# ---------------------------------------------------------------------------
# IFC4 — mm. 층 2(id 역순), 실 2, 기기 1(90° 회전), 벽 1(무시)
# ---------------------------------------------------------------------------

def _x2(s):
    return "\\X2\\" + "".join(f"{ord(c):04X}" for c in s) + "\\X0\\"


IFC = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [ReferenceView]'),'2;1');
FILE_NAME('plant.ifc','2026-01-01T00:00:00',(''),(''),'smr-ga fixture','smr-ga','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
#1=IFCPROJECT('0Proj000000000000000001',$,'Demo',$,$,$,$,(#9),#5);
#2=IFCSIUNIT(*,.LENGTHUNIT.,.MILLI.,.METRE.);
#3=IFCSIUNIT(*,.PLANEANGLEUNIT.,$,.RADIAN.);
#5=IFCUNITASSIGNMENT((#2,#3));
#9=IFCGEOMETRICREPRESENTATIONCONTEXT($,'Model',3,1.E-05,#10,$);
#10=IFCAXIS2PLACEMENT3D(#11,$,$);
#11=IFCCARTESIANPOINT((0.,0.,0.));
#12=IFCDIRECTION((0.,0.,1.));
#13=IFCDIRECTION((1.,0.,0.));
#14=IFCDIRECTION((0.,1.,0.));
#20=IFCSITE('0Site000000000000000001',$,'Site',$,$,#21,$,$,.ELEMENT.,$,$,$,$,$);
#21=IFCLOCALPLACEMENT($,#10);
#30=IFCBUILDING('0Bldg000000000000000001',$,'Aux Building',$,$,#31,$,$,.ELEMENT.,$,$,$);
#31=IFCLOCALPLACEMENT(#21,#10);
#40=IFCBUILDINGSTOREY('0Sty2000000000000000001',$,'Level 2',$,$,#41,$,$,.ELEMENT.,5000.);
#41=IFCLOCALPLACEMENT(#31,#42);
#42=IFCAXIS2PLACEMENT3D(#43,$,$);
#43=IFCCARTESIANPOINT((0.,0.,5000.));
#50=IFCBUILDINGSTOREY('0Sty1000000000000000001',$,'Level 1',$,$,#51,$,$,.ELEMENT.,0.);
#51=IFCLOCALPLACEMENT(#31,#10);
#60=IFCRELAGGREGATES('0Agg000000000000000001',$,$,$,#1,(#20));
#61=IFCRELAGGREGATES('0Agg000000000000000002',$,$,$,#20,(#30));
#62=IFCRELAGGREGATES('0Agg000000000000000003',$,$,$,#30,(#50,#40));
#100=IFCSPACE('0Spc101000000000000001',$,'101',$,$,#101,#105,'PUMP_ROOM_LONG',.ELEMENT.,.INTERNAL.,$);
#101=IFCLOCALPLACEMENT(#51,#102);
#102=IFCAXIS2PLACEMENT3D(#103,#12,#13);
#103=IFCCARTESIANPOINT((5000.,4000.,0.));
#104=IFCRECTANGLEPROFILEDEF(.AREA.,$,#106,8000.,6000.);
#106=IFCAXIS2PLACEMENT2D(#107,$);
#107=IFCCARTESIANPOINT((4000.,3000.));
#108=IFCEXTRUDEDAREASOLID(#104,#10,#12,4500.);
#109=IFCSHAPEREPRESENTATION(#9,'Body','SweptSolid',(#108));
#105=IFCPRODUCTDEFINITIONSHAPE($,$,(#109));
#200=IFCSPACE('0Spc201000000000000001',$,'201',$,$,#201,#205,'CTRL_LONG',.ELEMENT.,.INTERNAL.,$);
#201=IFCLOCALPLACEMENT(#41,#10);
#202=IFCPOLYLINE((#211,#212,#213,#214,#211));
#211=IFCCARTESIANPOINT((0.,0.));
#212=IFCCARTESIANPOINT((10000.,0.));
#213=IFCCARTESIANPOINT((10000.,5000.));
#214=IFCCARTESIANPOINT((0.,5000.));
#203=IFCARBITRARYCLOSEDPROFILEDEF(.AREA.,'ctrl',#202);
#204=IFCEXTRUDEDAREASOLID(#203,#10,#12,4000.);
#206=IFCSHAPEREPRESENTATION(#9,'Body','SweptSolid',(#204));
#205=IFCPRODUCTDEFINITIONSHAPE($,$,(#206));
#63=IFCRELAGGREGATES('0Agg000000000000000004',$,$,$,#50,(#100));
#64=IFCRELAGGREGATES('0Agg000000000000000005',$,$,$,#40,(#200));
#300=IFCBUILDINGELEMENTPROXY('0Prx301000000000000001',$,'P-301',$,'PUMP_TYPE',#301,#305,'P-301',$);
#301=IFCLOCALPLACEMENT(#101,#302);
#302=IFCAXIS2PLACEMENT3D(#303,#12,#14);
#303=IFCCARTESIANPOINT((2000.,3000.,0.));
#304=IFCRECTANGLEPROFILEDEF(.AREA.,$,#306,3000.,1000.);
#306=IFCAXIS2PLACEMENT2D(#11,$);
#307=IFCEXTRUDEDAREASOLID(#304,#10,#12,2500.);
#308=IFCSHAPEREPRESENTATION(#9,'Body','SweptSolid',(#307));
#305=IFCPRODUCTDEFINITIONSHAPE($,$,(#308));
#400=IFCWALL('0Wal401000000000000001',$,'Wall it''s ignored',$,$,#51,$,$,$);
#500=IFCRELCONTAINEDINSPATIALSTRUCTURE('0Rel000000000000000001',$,$,$,(#300,#400),#50);
ENDSEC;
END-ISO-10303-21;
"""


# ---------------------------------------------------------------------------
# 목록표
# ---------------------------------------------------------------------------

CSV = (
    "태그,명칭,폭(mm),깊이(mm),높이(mm),층,구역,이격(mm),중량(ton)\r\n"
    "P-101,냉각수 펌프 A,2000,1000,1500,1,RCA,600,3.5\r\n"
    "P-102,냉각수 펌프 B,2000,1000,1500,1,RCA,600,3.5\r\n"
    "TK-201,붕산수 탱크,3000,3000,4000,2,R2,800,12\r\n"
    "HX-301,열교환기,,1200,1500,2,R2,600,2\r\n"
)


def _xml_esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def make_xlsx():
    shared = ["기기 목록 (현황)", "ID", "Name", "W", "D", "X", "Y", "Floor", "Zone",
              "E-1", "펌프", "E-2", "탱크", "RCA"]
    si = {s: k for k, s in enumerate(shared)}
    rows = [
        [("s", "기기 목록 (현황)")],
        [("s", h) for h in ["ID", "Name", "W", "D", "X", "Y", "Floor", "Zone"]],
        [("s", "E-1"), ("s", "펌프"), ("n", "2.5"), ("n", "1.5"), ("n", "10"), ("n", "5"),
         ("n", "0"), ("s", "RCA")],
        [("s", "E-2"), ("s", "탱크"), ("n", "3"), ("n", "3"), ("n", "20"), ("n", "10"),
         ("n", "0"), ("inline", "NR")],
        [("s", "E-3"), ("s", "펌프"), ("n", "2"), ("n", "1.25E0"), ("n", "12"), ("n", "15"),
         ("n", "1"), None],
    ]
    if "E-3" not in si:
        si["E-3"] = len(shared); shared.append("E-3")

    def col(j):
        s = ""
        j += 1
        while j:
            j, r = divmod(j - 1, 26)
            s = chr(65 + r) + s
        return s
    sheet_rows = []
    for i, r in enumerate(rows, start=1):
        cells = []
        for j, c in enumerate(r):
            if c is None:
                continue
            ref = f"{col(j)}{i}"
            t, v = c
            if t == "s":
                cells.append(f'<c r="{ref}" t="s"><v>{si[v]}</v></c>')
            elif t == "inline":
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{_xml_esc(v)}</t></is></c>')
            else:
                cells.append(f'<c r="{ref}"><v>{v}</v></c>')
        sheet_rows.append(f'<row r="{i}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
            '</Types>'),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>'),
        "xl/workbook.xml": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook {ns} {rns}>'
            '<sheets><sheet name="기기목록" sheetId="1" r:id="rId1"/></sheets></workbook>'),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
            '</Relationships>'),
        "xl/sharedStrings.xml": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><sst {ns} count="{len(shared)}" uniqueCount="{len(shared)}">'
            + "".join(f"<si><t>{_xml_esc(s)}</t></si>" if s != "펌프" else
                      f"<si><r><t>펌</t></r><r><t>프</t></r></si>" for s in shared)
            + "</sst>"),
        "xl/worksheets/sheet1.xml": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet {ns}>'
            f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'),
    }
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):
            zi = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, files[name].encode("utf-8"))
    return buf.getvalue()


def main():
    os.makedirs(OUT, exist_ok=True)
    out = {
        "plant_mm.dxf": make_dxf(),
        "plant_cp949.dxf": make_dxf_cp949(),
        "plant.svg": SVG.encode("utf-8"),
        "plant.ifc": IFC.replace("PUMP_ROOM_LONG", _x2("펌프실")).replace("CTRL_LONG", _x2("제어실"))
                        .replace("PUMP_TYPE", _x2("펌프")).encode("ascii"),
        "equipment.csv": CSV.encode("cp949"),
        "equipment.xlsx": make_xlsx(),
    }
    for name in sorted(out):
        p = os.path.join(OUT, name)
        with open(p, "wb") as fp:
            fp.write(out[name])
        print(f"wrote {p} ({len(out[name])} B)")


if __name__ == "__main__":
    main()
