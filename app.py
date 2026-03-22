from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from sqlalchemy import text
from werkzeug.utils import secure_filename
import config
import os
import uuid

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = config.CONNECTION_STRING
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = config.SECRET_KEY
db = SQLAlchemy(app)

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
ALLOWED_EXTENSIONS = {'png','jpg','jpeg','gif','webp'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.',1)[1].lower() in ALLOWED_EXTENSIONS

def save_image(file):
    if file and allowed_file(file.filename):
        ext = file.filename.rsplit('.',1)[1].lower()
        fname = str(uuid.uuid4()) + '.' + ext
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], fname))
        return fname
    return None

# ════════════════════════════════════════════════════════════
#  MASTER DATA (Slide 9: Master vs Transaction Data)
# ════════════════════════════════════════════════════════════

class Musteri(db.Model):
    __tablename__ = 'Musteriler'
    id           = db.Column(db.Integer, primary_key=True)
    ad           = db.Column(db.String(100), nullable=False)
    telefon      = db.Column(db.String(20))
    email        = db.Column(db.String(100))
    adres        = db.Column(db.String(200))
    vergi_no     = db.Column(db.String(20))
    kredi_limiti = db.Column(db.Numeric(18,2), default=50000)  # Slide 10: Credit Limit
    is_active    = db.Column(db.Boolean, default=True)         # Slide 48: Soft Delete
    resim        = db.Column(db.String(200))
    olusturma    = db.Column(db.DateTime, default=datetime.utcnow)
    siparisler   = db.relationship('SatisSimdi', backref='musteri', lazy=True)

class Tedarikci(db.Model):
    __tablename__ = 'Tedarikciler'
    id        = db.Column(db.Integer, primary_key=True)
    ad        = db.Column(db.String(100), nullable=False)
    telefon   = db.Column(db.String(20))
    email     = db.Column(db.String(100))
    adres     = db.Column(db.String(200))
    vergi_no  = db.Column(db.String(20))
    is_active = db.Column(db.Boolean, default=True)            # Slide 48: Soft Delete
    resim     = db.Column(db.String(200))
    po_listesi = db.relationship('SatinAlmaSiparisi', backref='tedarikci', lazy=True)

class Kategori(db.Model):
    __tablename__ = 'Kategoriler'
    id   = db.Column(db.Integer, primary_key=True)
    ad   = db.Column(db.String(50), nullable=False)

class Urun(db.Model):
    # Slide 10: Material Master — Sales View + Purchasing View + MRP View
    __tablename__ = 'Urunler'
    id                    = db.Column(db.Integer, primary_key=True)
    ad                    = db.Column(db.String(100), nullable=False)
    sku                   = db.Column(db.String(50), unique=True)      # Slide 51: SKU
    kategori_id           = db.Column(db.Integer, db.ForeignKey('Kategoriler.id'))
    # Sales View
    satis_fiyati          = db.Column(db.Numeric(18,2), nullable=False)
    # Purchasing View
    tercihli_tedarikci_id = db.Column(db.Integer, db.ForeignKey('Tedarikciler.id'))
    # MRP View (Slide 10: Safety Stock, Lead Time)
    stok                  = db.Column(db.Integer, default=0)
    minimum_stok          = db.Column(db.Integer, default=10)
    yenileme_miktari      = db.Column(db.Integer, default=50)
    temin_suresi_gun      = db.Column(db.Integer, default=2)
    birim                 = db.Column(db.String(20), default='Adet')
    # Cost (Slide 24, 41: FIFO/LIFO/Average MAP)
    maliyet_yontemi       = db.Column(db.String(10), default='FIFO')
    ortalama_maliyet      = db.Column(db.Numeric(18,4), default=0)
    resim                 = db.Column(db.String(200))                   # Ürün görseli
    urun_tipi             = db.Column(db.String(20), default='TICARI')  # HAMMADDE/YARI_MAMUL/MAMUL/TICARI
    is_active             = db.Column(db.Boolean, default=True)        # Slide 48: Soft Delete
    kategori              = db.relationship('Kategori', backref='urunler')
    lotlar                = db.relationship('StokLot', backref='urun', lazy=True,
                                            order_by='StokLot.giris_tarihi')

    @property
    def mevcut_maliyet(self):
        if self.maliyet_yontemi == 'AVERAGE':
            return float(self.ortalama_maliyet or 0)
        lots = [l for l in self.lotlar if l.kalan > 0]
        if not lots:
            return float(self.ortalama_maliyet or self.satis_fiyati * 0.7)
        return float(lots[0].birim_maliyet) if self.maliyet_yontemi == 'FIFO' else float(lots[-1].birim_maliyet)

# Slide 24/41: Stock Lots for FIFO/LIFO/Average
class StokLot(db.Model):
    __tablename__ = 'StokLotlari'
    id            = db.Column(db.Integer, primary_key=True)
    urun_id       = db.Column(db.Integer, db.ForeignKey('Urunler.id'), nullable=False)
    giris_tarihi  = db.Column(db.DateTime, default=datetime.utcnow)
    miktar        = db.Column(db.Integer, nullable=False)
    kalan         = db.Column(db.Integer, nullable=False)
    birim_maliyet = db.Column(db.Numeric(18,4), nullable=False)
    kaynak        = db.Column(db.String(100))

# ════════════════════════════════════════════════════════════
#  TRANSACTION DATA — ORDER-TO-CASH
#  Slide 19: Draft(00) → Confirmed(01) → Picking(02) → Shipped(03) → Invoiced(04)
# ════════════════════════════════════════════════════════════

# Slide 19: State Machine — Order Status Codes
ORDER_STATES = {
    '00_TASLAK':    'Taslak',
    '01_ONAYLANDI': 'Onaylandı',
    '02_PICKING':   'Hazırlanıyor',
    '03_SEVK':      'Sevk Edildi',
    '04_FATURALAND':'Faturalandı',
    'XX_IPTAL':     'İptal'
}

class SatisSimdi(db.Model):
    # Slide 17: SALES_HEADER (VBAK)
    __tablename__ = 'SatisSimdi'
    id             = db.Column(db.Integer, primary_key=True)
    siparis_no     = db.Column(db.String(20), unique=True)
    musteri_id     = db.Column(db.Integer, db.ForeignKey('Musteriler.id'), nullable=False)
    tarih          = db.Column(db.DateTime, default=datetime.utcnow)
    durum          = db.Column(db.String(20), default='00_TASLAK')      # State Machine
    odeme_kosulu   = db.Column(db.String(50), default='Peşin')          # Slide 17: Payment_Terms
    notlar         = db.Column(db.Text)
    sevk_tarihi    = db.Column(db.DateTime)
    fatura_tarihi  = db.Column(db.DateTime)
    kalemler       = db.relationship('SatisKalemi', backref='siparis', lazy=True,
                                     cascade='all, delete-orphan')
    fatura         = db.relationship('SatisFaturasi', backref='siparis', uselist=False)

    @property
    def toplam(self):
        return float(sum(float(k.miktar) * float(k.birim_fiyat) for k in self.kalemler))

    @property
    def durum_label(self):
        return ORDER_STATES.get(self.durum, self.durum)

    @property
    def fatura_gerekli(self):
        return self.durum == '03_SEVK' and not self.fatura

class SatisKalemi(db.Model):
    # Slide 17: SALES_LINE ITEMS
    __tablename__ = 'SatisKalemleri'
    id          = db.Column(db.Integer, primary_key=True)
    siparis_id  = db.Column(db.Integer, db.ForeignKey('SatisSimdi.id'), nullable=False)
    urun_id     = db.Column(db.Integer, db.ForeignKey('Urunler.id'), nullable=False)
    miktar      = db.Column(db.Integer, nullable=False)
    birim_fiyat = db.Column(db.Numeric(18,2), nullable=False)  # Slide 16: Static Copy
    maliyet     = db.Column(db.Numeric(18,4), default=0)       # COGS at time of sale
    urun        = db.relationship('Urun')

    @property
    def toplam(self):
        return float(self.miktar) * float(self.birim_fiyat)

    @property
    def kar(self):
        return self.toplam - float(self.maliyet or 0) * float(self.miktar)

class SatisFaturasi(db.Model):
    __tablename__ = 'SatisFaturalari'
    id            = db.Column(db.Integer, primary_key=True)
    fatura_no     = db.Column(db.String(20), unique=True)
    siparis_id    = db.Column(db.Integer, db.ForeignKey('SatisSimdi.id'), nullable=False)
    tarih         = db.Column(db.DateTime, default=datetime.utcnow)
    odeme_durumu  = db.Column(db.String(20), default='Ödenmedi')
    vade_tarihi   = db.Column(db.DateTime)
    kdv_orani     = db.Column(db.Numeric(5,2), default=0.20)

    @property
    def ara_toplam(self): return float(self.siparis.toplam)
    @property
    def kdv_tutari(self): return float(self.ara_toplam) * float(self.kdv_orani)
    @property
    def genel_toplam(self): return self.ara_toplam + self.kdv_tutari

# ════════════════════════════════════════════════════════════
#  TRANSACTION DATA — PROCURE-TO-PAY
#  Slide 33-40: PR → PO → GRN → Vendor Invoice → Payment
# ════════════════════════════════════════════════════════════

PO_STATES = {
    'TASLAK':       'Taslak (PR)',
    'ONAYLANDI':    'Onaylandı (PO)',
    'GONDERILDI':   'Gönderildi',
    'GRN_BEKLENDI': 'Teslimat Bekleniyor',
    'TESLIM_ALINDI':'Teslim Alındı (GRN)',
    'FATURA_ESLESTI':'3-Way Match ✓',
    'ODENDI':       'Ödendi',
    'IPTAL':        'İptal'
}

class SatinAlmaSiparisi(db.Model):
    # Slide 38: PO_HEADER
    __tablename__ = 'SatinAlmaSiparisleri'
    id            = db.Column(db.Integer, primary_key=True)
    po_no         = db.Column(db.String(20), unique=True)
    tedarikci_id  = db.Column(db.Integer, db.ForeignKey('Tedarikciler.id'))
    tarih         = db.Column(db.DateTime, default=datetime.utcnow)
    durum         = db.Column(db.String(20), default='TASLAK')
    otomatik      = db.Column(db.Boolean, default=False)      # Slide 35: MRP Auto
    notlar        = db.Column(db.Text)
    grn_tarihi    = db.Column(db.DateTime)                    # Slide 39: GRN date
    kalemler      = db.relationship('SatinAlmaKalemi', backref='po', lazy=True,
                                    cascade='all, delete-orphan')
    vendor_invoice = db.relationship('VendorInvoice', backref='po', uselist=False)

    @property
    def toplam(self):
        return float(sum(float(k.miktar) * float(k.birim_maliyet) for k in self.kalemler))

    @property
    def durum_label(self):
        return PO_STATES.get(self.durum, self.durum)

class SatinAlmaKalemi(db.Model):
    # Slide 38: PO_LINES
    __tablename__ = 'SatinAlmaKalemleri'
    id            = db.Column(db.Integer, primary_key=True)
    po_id         = db.Column(db.Integer, db.ForeignKey('SatinAlmaSiparisleri.id'), nullable=False)
    urun_id       = db.Column(db.Integer, db.ForeignKey('Urunler.id'), nullable=False)
    miktar        = db.Column(db.Integer, nullable=False)
    birim_maliyet = db.Column(db.Numeric(18,4), nullable=False)
    teslim_miktar = db.Column(db.Integer, default=0)           # GRN received qty
    urun          = db.relationship('Urun')

class VendorInvoice(db.Model):
    # Slide 42: 3-Way Match — Vendor Invoice (Billed Cost)
    __tablename__ = 'VendorInvoiceler'
    id             = db.Column(db.Integer, primary_key=True)
    po_id          = db.Column(db.Integer, db.ForeignKey('SatinAlmaSiparisleri.id'), nullable=False)
    invoice_no     = db.Column(db.String(50))
    tarih          = db.Column(db.DateTime, default=datetime.utcnow)
    toplam_tutar   = db.Column(db.Numeric(18,2), nullable=False)
    match_durumu   = db.Column(db.String(20), default='Beklemede')  # Eşleşti / Bloklandı
    notlar         = db.Column(db.Text)

# ════════════════════════════════════════════════════════════
#  GENERAL LEDGER (Slide 28, 55: Double-Entry)
# ════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════
#  ÜRETİM MODÜLLERİ — MRP / BOM / Work Order
#  Slide 36: MRP Calculation Engine
# ════════════════════════════════════════════════════════════

# Ürün tipi için Urun modeline uretim_tipi ekliyoruz:
# 'HAMMADDE' | 'YARI_MAMUL' | 'MAMUL' | 'TICARI' (varsayılan)
# Bu alanı migration yerine Urun tablosuna ekleyeceğiz

class BOM(db.Model):
    # Bill of Materials — hangi hammaddeden ne kadar lazım
    __tablename__ = 'BOM'
    id          = db.Column(db.Integer, primary_key=True)
    mamul_id    = db.Column(db.Integer, db.ForeignKey('Urunler.id'), nullable=False)
    versiyon    = db.Column(db.String(10), default='1.0')
    aciklama    = db.Column(db.String(200))
    aktif       = db.Column(db.Boolean, default=True)
    olusturma   = db.Column(db.DateTime, default=datetime.utcnow)
    mamul       = db.relationship('Urun', foreign_keys=[mamul_id], backref='bom_listesi')
    kalemler    = db.relationship('BOMKalemi', backref='bom', lazy=True, cascade='all, delete-orphan')

    @property
    def toplam_maliyet(self):
        return float(sum(float(k.miktar) * float(k.hammadde.mevcut_maliyet) for k in self.kalemler))

class BOMKalemi(db.Model):
    # BOM satırları — her hammadde için miktar
    __tablename__ = 'BOMKalemleri'
    id           = db.Column(db.Integer, primary_key=True)
    bom_id       = db.Column(db.Integer, db.ForeignKey('BOM.id'), nullable=False)
    hammadde_id  = db.Column(db.Integer, db.ForeignKey('Urunler.id'), nullable=False)
    miktar       = db.Column(db.Numeric(18,4), nullable=False)  # 1 mamul için gereken miktar
    birim        = db.Column(db.String(20), default='Adet')
    fire_yuzdesi = db.Column(db.Numeric(5,2), default=0)        # fire/kayıp yüzdesi
    lead_time_gun = db.Column(db.Integer, default=0)             # bu malzeme için temin süresi (gün)
    notlar       = db.Column(db.String(200))                     # BOM satır notu
    hammadde     = db.relationship('Urun', foreign_keys=[hammadde_id])

    @property
    def net_miktar(self):
        # Fire dahil gereken miktar
        return float(self.miktar) * (1 + float(self.fire_yuzdesi or 0) / 100)

# Slide 36: MRP — üretim emri
UE_STATES = {
    'TASLAK':     'Taslak',
    'PLANLI':     'Planlandı',
    'URETIMDE':   'Üretimde',
    'TAMAMLANDI': 'Tamamlandı',
    'IPTAL':      'İptal'
}

class UretimEmri(db.Model):
    __tablename__ = 'UretimEmirleri'
    id              = db.Column(db.Integer, primary_key=True)
    ue_no           = db.Column(db.String(20), unique=True)
    mamul_id        = db.Column(db.Integer, db.ForeignKey('Urunler.id'), nullable=False)
    bom_id          = db.Column(db.Integer, db.ForeignKey('BOM.id'), nullable=False)
    uretim_miktari  = db.Column(db.Integer, nullable=False)
    durum           = db.Column(db.String(20), default='TASLAK')
    planlanan_baslangic = db.Column(db.DateTime)
    planlanan_bitis     = db.Column(db.DateTime)
    gercek_baslangic    = db.Column(db.DateTime)
    gercek_bitis        = db.Column(db.DateTime)
    notlar          = db.Column(db.Text)
    olusturma       = db.Column(db.DateTime, default=datetime.utcnow)
    mrp_kaynagi     = db.Column(db.Boolean, default=False)  # MRP tarafından mı oluşturuldu?
    mamul           = db.relationship('Urun', foreign_keys=[mamul_id])
    bom             = db.relationship('BOM')
    malzeme_hareketleri = db.relationship('UEMalzemeHareketi', backref='ue', lazy=True)

    @property
    def durum_label(self):
        return UE_STATES.get(self.durum, self.durum)

    @property
    def toplam_maliyet(self):
        return float(sum(float(h.miktar) * float(h.birim_maliyet) for h in self.malzeme_hareketleri))

class UEMalzemeHareketi(db.Model):
    # Üretim emrinde tüketilen hammaddeler
    __tablename__ = 'UEMalzemeHareketleri'
    id            = db.Column(db.Integer, primary_key=True)
    ue_id         = db.Column(db.Integer, db.ForeignKey('UretimEmirleri.id'), nullable=False)
    hammadde_id   = db.Column(db.Integer, db.ForeignKey('Urunler.id'), nullable=False)
    miktar        = db.Column(db.Numeric(18,4), nullable=False)
    birim_maliyet = db.Column(db.Numeric(18,4), default=0)
    tarih         = db.Column(db.DateTime, default=datetime.utcnow)
    hammadde      = db.relationship('Urun')

class GenelDefter(db.Model):
    # Slide 28: General_Ledger — COGS (Expense) + Inventory (Asset)
    __tablename__ = 'GenelDefter'
    id           = db.Column(db.Integer, primary_key=True)
    tarih        = db.Column(db.DateTime, default=datetime.utcnow)
    hesap        = db.Column(db.String(50))   # COGS / Inventory_Asset / AR / Revenue
    borc         = db.Column(db.Numeric(18,2), default=0)   # Debit
    alacak       = db.Column(db.Numeric(18,2), default=0)   # Credit
    ref_id       = db.Column(db.Integer)      # Order/PO ID for traceability
    ref_tip      = db.Column(db.String(20))   # 'SATIS' / 'PO'
    aciklama     = db.Column(db.String(200))

# ════════════════════════════════════════════════════════════
#  AUDIT TRAIL (Slide 14: Who, What, When)
# ════════════════════════════════════════════════════════════

class AuditLog(db.Model):
    __tablename__ = 'AuditLog'
    id         = db.Column(db.Integer, primary_key=True)
    tarih      = db.Column(db.DateTime, default=datetime.utcnow)
    tablo      = db.Column(db.String(50))
    kayit_id   = db.Column(db.Integer)
    aksiyon    = db.Column(db.String(50))    # Status Change, Create, Update
    eski_deger = db.Column(db.String(100))
    yeni_deger = db.Column(db.String(100))
    aciklama   = db.Column(db.String(200))

class StokHareketi(db.Model):
    __tablename__ = 'StokHareketleri'
    id        = db.Column(db.Integer, primary_key=True)
    urun_id   = db.Column(db.Integer, db.ForeignKey('Urunler.id'), nullable=False)
    tarih     = db.Column(db.DateTime, default=datetime.utcnow)
    tur       = db.Column(db.String(20))
    miktar    = db.Column(db.Integer)
    aciklama  = db.Column(db.String(200))
    urun      = db.relationship('Urun')

# ════════════════════════════════════════════════════════════
#  YARDIMCI FONKSİYONLAR
# ════════════════════════════════════════════════════════════

def siparis_no_uret():
    son = SatisSimdi.query.order_by(SatisSimdi.id.desc()).first()
    return f"S{(son.id+1 if son else 1):05d}"

def fatura_no_uret():
    son = SatisFaturasi.query.order_by(SatisFaturasi.id.desc()).first()
    return f"F{(son.id+1 if son else 1):05d}"

def po_no_uret():
    son = SatinAlmaSiparisi.query.order_by(SatinAlmaSiparisi.id.desc()).first()
    return f"PO{(son.id+1 if son else 1):05d}"

def audit(tablo, kayit_id, aksiyon, eski='', yeni='', aciklama=''):
    db.session.add(AuditLog(tablo=tablo, kayit_id=kayit_id, aksiyon=aksiyon,
        eski_deger=str(eski), yeni_deger=str(yeni), aciklama=aciklama))

def gl_yaz(hesap, borc, alacak, ref_id, ref_tip, aciklama=''):
    # Slide 28/55: Double-Entry GL
    db.session.add(GenelDefter(hesap=hesap, borc=borc, alacak=alacak,
        ref_id=ref_id, ref_tip=ref_tip, aciklama=aciklama))

def stok_lot_ekle(urun, miktar, birim_maliyet, kaynak=''):
    # Slide 41: Moving Average Price (MAP) recalculation
    lot = StokLot(urun_id=urun.id, miktar=miktar, kalan=miktar,
                  birim_maliyet=birim_maliyet, kaynak=kaynak)
    db.session.add(lot)
    eski_stok = urun.stok
    eski_maliyet = float(urun.ortalama_maliyet or 0)
    toplam_deger = (eski_stok * eski_maliyet) + (miktar * float(birim_maliyet))
    urun.stok += miktar
    yeni_avg = toplam_deger / urun.stok if urun.stok > 0 else float(birim_maliyet)
    urun.ortalama_maliyet = yeni_avg

def stok_lot_dus(urun, miktar):
    # Slide 24: FIFO/LIFO/Average cost deduction
    kalan = miktar
    toplam_maliyet = 0.0
    if urun.maliyet_yontemi == 'AVERAGE':
        toplam_maliyet = float(urun.ortalama_maliyet or 0) * miktar
        lotlar = sorted([l for l in urun.lotlar if l.kalan > 0], key=lambda x: x.giris_tarihi)
        for lot in lotlar:
            if kalan <= 0: break
            al = min(lot.kalan, kalan); lot.kalan -= al; kalan -= al
    else:
        lotlar = sorted([l for l in urun.lotlar if l.kalan > 0], key=lambda x: x.giris_tarihi)
        if urun.maliyet_yontemi == 'LIFO': lotlar = list(reversed(lotlar))
        for lot in lotlar:
            if kalan <= 0: break
            al = min(lot.kalan, kalan)
            toplam_maliyet += al * float(lot.birim_maliyet)
            lot.kalan -= al; kalan -= al
    urun.stok -= miktar
    return toplam_maliyet / miktar if miktar > 0 else 0

def otomatik_po_olustur():
    # Slide 35/36: MRP Algorithm — auto PR when stock < reorder point
    urunler = Urun.query.filter(Urun.stok <= Urun.minimum_stok, Urun.is_active == True).all()
    olusturulan = []
    for u in urunler:
        mevcut = db.session.query(SatinAlmaKalemi).join(SatinAlmaSiparisi)\
            .filter(SatinAlmaSiparisi.durum.in_(['TASLAK','ONAYLANDI','GONDERILDI','GRN_BEKLENDI']))\
            .filter(SatinAlmaKalemi.urun_id == u.id).first()
        if not mevcut:
            po = SatinAlmaSiparisi(po_no=po_no_uret(),
                tedarikci_id=u.tercihli_tedarikci_id, otomatik=True,
                notlar=f'MRP otomatik: {u.ad} stok={u.stok}, min={u.minimum_stok}')
            db.session.add(po); db.session.flush()
            db.session.add(SatinAlmaKalemi(po_id=po.id, urun_id=u.id,
                miktar=u.yenileme_miktari,
                birim_maliyet=u.ortalama_maliyet or u.satis_fiyati * 0.7))
            audit('SatinAlmaSiparisleri', po.id, 'MRP_AUTO_CREATE', '', 'TASLAK',
                  f'{u.ad} için otomatik PO')
            olusturulan.append(u.ad)
    if olusturulan: db.session.commit()
    return olusturulan

# ════════════════════════════════════════════════════════════
#  DASHBOARD
# ════════════════════════════════════════════════════════════

@app.route('/')
def dashboard():
    fatura_gerekli = [s for s in SatisSimdi.query.filter_by(durum='03_SEVK').all() if not s.fatura]
    return render_template('dashboard.html',
        toplam_musteri   = Musteri.query.filter_by(is_active=True).count(),
        toplam_siparis   = SatisSimdi.query.count(),
        bekleyen         = SatisSimdi.query.filter_by(durum='00_TASLAK').count(),
        picking          = SatisSimdi.query.filter_by(durum='02_PICKING').count(),
        odenmemis        = SatisFaturasi.query.filter_by(odeme_durumu='Ödenmedi').count(),
        odenmemis_tutar  = sum(f.genel_toplam for f in SatisFaturasi.query.filter_by(odeme_durumu='Ödenmedi')),
        kritik_stok      = Urun.query.filter(Urun.stok <= Urun.minimum_stok, Urun.is_active==True).all(),
        fatura_gerekli   = fatura_gerekli,
        bekleyen_po      = SatinAlmaSiparisi.query.filter(
                             SatinAlmaSiparisi.durum.in_(['TASLAK','ONAYLANDI','GONDERILDI','GRN_BEKLENDI'])).count(),
        son_siparisler   = SatisSimdi.query.order_by(SatisSimdi.tarih.desc()).limit(8).all(),
        son_po           = SatinAlmaSiparisi.query.order_by(SatinAlmaSiparisi.tarih.desc()).limit(5).all(),
        son_gl           = GenelDefter.query.order_by(GenelDefter.tarih.desc()).limit(8).all(),
        now              = datetime.now())

# ════════════════════════════════════════════════════════════
#  MÜŞTERİ (Soft Delete)
# ════════════════════════════════════════════════════════════

@app.route('/musteriler')
def musteriler():
    q = request.args.get('q','')
    liste = Musteri.query.filter(Musteri.is_active==True, Musteri.ad.ilike(f'%{q}%')).all()
    return render_template('musteriler.html', musteriler=liste, q=q)

@app.route('/musteri/yeni', methods=['GET','POST'])
def musteri_yeni():
    if request.method == 'POST':
        resim = save_image(request.files.get('resim'))
        m = Musteri(ad=request.form['ad'], telefon=request.form['telefon'],
            email=request.form['email'], adres=request.form['adres'],
            vergi_no=request.form['vergi_no'],
            kredi_limiti=float(request.form.get('kredi_limiti',50000)),
            resim=resim)
        db.session.add(m); db.session.flush()
        audit('Musteriler', m.id, 'CREATE', '', m.ad)
        db.session.commit(); flash('Müşteri eklendi.','success')
        return redirect(url_for('musteriler'))
    return render_template('musteri_form.html', musteri=None)

@app.route('/musteri/<int:id>/duzenle', methods=['GET','POST'])
def musteri_duzenle(id):
    m = Musteri.query.get_or_404(id)
    if request.method == 'POST':
        m.ad=request.form['ad']; m.telefon=request.form['telefon']
        m.email=request.form['email']; m.adres=request.form['adres']
        m.vergi_no=request.form['vergi_no']
        m.kredi_limiti=float(request.form.get('kredi_limiti',50000))
        yeni_resim = save_image(request.files.get('resim'))
        if yeni_resim: m.resim = yeni_resim
        audit('Musteriler', m.id, 'UPDATE')
        db.session.commit(); flash('Güncellendi.','success')
        return redirect(url_for('musteriler'))
    return render_template('musteri_form.html', musteri=m)

@app.route('/musteri/<int:id>/sil', methods=['POST'])
def musteri_sil(id):
    m = Musteri.query.get_or_404(id)
    m.is_active = False  # Slide 48: Soft Delete
    audit('Musteriler', m.id, 'SOFT_DELETE', 'Active', 'Inactive')
    db.session.commit(); flash('Müşteri pasife alındı (soft delete).','warning')
    return redirect(url_for('musteriler'))

# ════════════════════════════════════════════════════════════
#  TEDARİKÇİ
# ════════════════════════════════════════════════════════════

@app.route('/tedarikciler')
def tedarikciler():
    return render_template('tedarikciler.html',
        tedarikciler=Tedarikci.query.filter_by(is_active=True).all())

@app.route('/tedarikci/yeni', methods=['GET','POST'])
def tedarikci_yeni():
    if request.method == 'POST':
        resim = save_image(request.files.get('resim'))
        t = Tedarikci(ad=request.form['ad'], telefon=request.form['telefon'],
            email=request.form['email'], adres=request.form['adres'],
            vergi_no=request.form['vergi_no'], resim=resim)
        db.session.add(t); db.session.commit(); flash('Tedarikçi eklendi.','success')
        return redirect(url_for('tedarikciler'))
    return render_template('tedarikci_form.html', tedarikci=None)

@app.route('/tedarikci/<int:id>/duzenle', methods=['GET','POST'])
def tedarikci_duzenle(id):
    t = Tedarikci.query.get_or_404(id)
    if request.method == 'POST':
        t.ad=request.form['ad']; t.telefon=request.form['telefon']
        t.email=request.form['email']; t.adres=request.form['adres']; t.vergi_no=request.form['vergi_no']
        db.session.commit(); flash('Güncellendi.','success')
        return redirect(url_for('tedarikciler'))
    return render_template('tedarikci_form.html', tedarikci=t)

# ════════════════════════════════════════════════════════════
#  ÜRÜN
# ════════════════════════════════════════════════════════════

@app.route('/urunler')
def urunler():
    q = request.args.get('q','')
    liste = Urun.query.filter(Urun.is_active==True, Urun.ad.ilike(f'%{q}%')).all()
    return render_template('urunler.html', urunler=liste, q=q)

@app.route('/urun/yeni', methods=['GET','POST'])
def urun_yeni():
    if request.method == 'POST':
        resim = save_image(request.files.get('resim'))
        u = Urun(ad=request.form['ad'], sku=request.form.get('sku',''),
            kategori_id=request.form.get('kategori_id') or None,
            satis_fiyati=float(request.form['satis_fiyati']),
            stok=0, minimum_stok=int(request.form['minimum_stok']),
            yenileme_miktari=int(request.form['yenileme_miktari']),
            temin_suresi_gun=int(request.form.get('temin_suresi_gun',2)),
            birim=request.form['birim'],
            maliyet_yontemi=request.form['maliyet_yontemi'],
            urun_tipi=request.form.get('urun_tipi','TICARI'),
            tercihli_tedarikci_id=request.form.get('tedarikci_id') or None,
            resim=resim)
        db.session.add(u); db.session.flush()
        ilk_stok = int(request.form.get('ilk_stok',0))
        ilk_maliyet = float(request.form.get('ilk_maliyet',0) or 0)
        if ilk_stok > 0 and ilk_maliyet > 0:
            stok_lot_ekle(u, ilk_stok, ilk_maliyet, 'Başlangıç stoğu')
            db.session.add(StokHareketi(urun_id=u.id, tur='Giriş',
                miktar=ilk_stok, aciklama='Başlangıç stoğu'))
        audit('Urunler', u.id, 'CREATE', '', u.ad)
        db.session.commit(); flash('Ürün eklendi.','success')
        return redirect(url_for('urunler'))
    return render_template('urun_form.html', urun=None,
        kategoriler=Kategori.query.all(), tedarikciler=Tedarikci.query.filter_by(is_active=True).all())

@app.route('/urun/<int:id>/duzenle', methods=['GET','POST'])
def urun_duzenle(id):
    u = Urun.query.get_or_404(id)
    if request.method == 'POST':
        u.ad=request.form['ad']; u.sku=request.form.get('sku','')
        u.kategori_id=request.form.get('kategori_id') or None
        u.satis_fiyati=float(request.form['satis_fiyati'])
        u.minimum_stok=int(request.form['minimum_stok'])
        u.yenileme_miktari=int(request.form['yenileme_miktari'])
        u.temin_suresi_gun=int(request.form.get('temin_suresi_gun',2))
        u.birim=request.form['birim']
        u.maliyet_yontemi=request.form['maliyet_yontemi']
        u.urun_tipi=request.form.get('urun_tipi','TICARI')
        u.tercihli_tedarikci_id=request.form.get('tedarikci_id') or None
        yeni_resim = save_image(request.files.get('resim'))
        if yeni_resim: u.resim = yeni_resim
        audit('Urunler', u.id, 'UPDATE')
        db.session.commit(); flash('Ürün güncellendi.','success')
        return redirect(url_for('urunler'))
    return render_template('urun_form.html', urun=u,
        kategoriler=Kategori.query.all(), tedarikciler=Tedarikci.query.filter_by(is_active=True).all())

@app.route('/urun/<int:id>')
def urun_detay(id):
    u = Urun.query.get_or_404(id)
    return render_template('urun_detay.html', urun=u,
        hareketler=StokHareketi.query.filter_by(urun_id=id).order_by(StokHareketi.tarih.desc()).limit(20).all())

# ════════════════════════════════════════════════════════════
#  SİPARİŞ — ORDER-TO-CASH STATE MACHINE
#  Slide 19: 00_TASLAK → 01_ONAYLANDI → 02_PICKING → 03_SEVK → 04_FATURALAND
# ════════════════════════════════════════════════════════════

@app.route('/siparisler')
def siparisler():
    durum = request.args.get('durum','')
    q = SatisSimdi.query.order_by(SatisSimdi.tarih.desc())
    if durum: q = q.filter_by(durum=durum)
    return render_template('siparisler.html', siparisler=q.all(), durum=durum, order_states=ORDER_STATES)

@app.route('/siparis/yeni', methods=['GET','POST'])
def siparis_yeni():
    if request.method == 'POST':
        s = SatisSimdi(siparis_no=siparis_no_uret(),
            musteri_id=int(request.form['musteri_id']),
            odeme_kosulu=request.form.get('odeme_kosulu','Peşin'),
            notlar=request.form.get('notlar',''))
        db.session.add(s); db.session.flush()
        for uid, mik in zip(request.form.getlist('urun_id'), request.form.getlist('miktar')):
            if uid and mik and int(mik) > 0:
                u = Urun.query.get(int(uid))
                db.session.add(SatisKalemi(siparis_id=s.id, urun_id=int(uid),
                    miktar=int(mik), birim_fiyat=u.satis_fiyati))  # Slide 16: Static Copy
        audit('SatisSimdi', s.id, 'CREATE', '', '00_TASLAK', f'{s.siparis_no} oluşturuldu')
        db.session.commit(); flash(f'{s.siparis_no} oluşturuldu (Taslak).','success')
        return redirect(url_for('siparis_detay', id=s.id))
    return render_template('siparis_form.html',
        musteriler=Musteri.query.filter_by(is_active=True).all(),
        urunler=Urun.query.filter_by(is_active=True).all())

@app.route('/siparis/<int:id>')
def siparis_detay(id):
    return render_template('siparis_detay.html',
        siparis=SatisSimdi.query.get_or_404(id), order_states=ORDER_STATES)

@app.route('/siparis/<int:id>/onayla', methods=['POST'])
def siparis_onayla(id):
    # Slide 19: 00 → 01 Confirmed + Price Lock + ATP Check (Slide 21)
    s = SatisSimdi.query.get_or_404(id)
    if s.durum != '00_TASLAK':
        flash('Sadece Taslak siparişler onaylanabilir.','danger'); return redirect(url_for('siparis_detay', id=id))
    # ATP Check (Slide 21)
    for k in s.kalemler:
        if k.urun.stok < k.miktar:
            flash(f'Yetersiz stok: {k.urun.ad} (Mevcut: {k.urun.stok}, Talep: {k.miktar})','danger')
            return redirect(url_for('siparis_detay', id=id))
    # Slide 57-62: ACID Transaction
    try:
        eski = s.durum
        s.durum = '01_ONAYLANDI'
        audit('SatisSimdi', s.id, 'STATUS_CHANGE', eski, '01_ONAYLANDI', 'Sipariş onaylandı, fiyat kilitlendi')
        db.session.commit()
        flash(f'{s.siparis_no} onaylandı. Fiyatlar kilitlendi (Confirmed 01).','success')
    except Exception as e:
        db.session.rollback(); flash(f'Hata (ROLLBACK): {str(e)}','danger')
    return redirect(url_for('siparis_detay', id=id))

@app.route('/siparis/<int:id>/picking', methods=['POST'])
def siparis_picking(id):
    # Slide 19: 01 → 02 Picking — stok fiziksel olarak ayrılır
    s = SatisSimdi.query.get_or_404(id)
    if s.durum != '01_ONAYLANDI':
        flash('Sadece Onaylandi siparisler pickinge gecebilir.','danger'); return redirect(url_for('siparis_detay', id=id))
    try:
        eski = s.durum
        s.durum = '02_PICKING'
        # ACID: Stok düş + Stok hareketi + GL entry tek transaction
        for k in s.kalemler:
            maliyet = stok_lot_dus(k.urun, k.miktar)   # FIFO/LIFO/AVG
            k.maliyet = maliyet
            db.session.add(StokHareketi(urun_id=k.urun_id, tur='Çıkış',
                miktar=k.miktar, aciklama=f'Picking: {s.siparis_no}'))
            # Slide 28/55: Double-Entry GL — COGS Debit + Inventory Credit
            gl_yaz('COGS', k.toplam, 0, s.id, 'SATIS', f'{s.siparis_no} - {k.urun.ad}')
            gl_yaz('Stok_Varlik', 0, float(maliyet)*k.miktar, s.id, 'SATIS', f'{s.siparis_no} - {k.urun.ad}')
        audit('SatisSimdi', s.id, 'STATUS_CHANGE', eski, '02_PICKING', 'Stok düşüldü, GL yazıldı')
        db.session.commit()
        oto = otomatik_po_olustur()
        if oto: flash(f'MRP: {", ".join(oto)} için otomatik PO oluşturuldu!','warning')
        flash(f'{s.siparis_no} hazırlanıyor (Picking 02). Stok ve GL güncellendi.','success')
    except Exception as e:
        db.session.rollback(); flash(f'ROLLBACK: {str(e)}','danger')
    return redirect(url_for('siparis_detay', id=id))

@app.route('/siparis/<int:id>/sevk', methods=['POST'])
def siparis_sevk(id):
    # Slide 19: 02 → 03 Shipped + Quantity Lock
    s = SatisSimdi.query.get_or_404(id)
    if s.durum != '02_PICKING':
        flash('Sadece Hazırlanıyor siparişler sevk edilebilir.','danger'); return redirect(url_for('siparis_detay', id=id))
    try:
        eski = s.durum
        s.durum = '03_SEVK'
        s.sevk_tarihi = datetime.utcnow()
        audit('SatisSimdi', s.id, 'STATUS_CHANGE', eski, '03_SEVK', 'Miktar kilitlendi')
        db.session.commit()
        flash(f'{s.siparis_no} sevk edildi (Shipped 03). ⚠️ Fatura oluşturmayı unutmayın!','warning')
    except Exception as e:
        db.session.rollback(); flash(f'ROLLBACK: {str(e)}','danger')
    return redirect(url_for('siparis_detay', id=id))

@app.route('/siparis/<int:id>/fatura-olustur', methods=['POST'])
def satis_fatura_olustur(id):
    # Slide 19: 03 → 04 Invoiced
    s = SatisSimdi.query.get_or_404(id)
    if s.fatura:
        return redirect(url_for('satis_fatura_detay', id=s.fatura.id))
    try:
        f = SatisFaturasi(fatura_no=fatura_no_uret(), siparis_id=id)
        db.session.add(f); db.session.flush()
        s.durum = '04_FATURALAND'
        s.fatura_tarihi = datetime.utcnow()
        # GL: AR Debit + Revenue Credit
        gl_yaz('Alacak_Hesaplari', f.genel_toplam, 0, s.id, 'SATIS', f'{f.fatura_no} AR')
        gl_yaz('Satis_Geliri', 0, s.toplam, s.id, 'SATIS', f'{f.fatura_no} Revenue')
        gl_yaz('KDV_Borcu', 0, f.kdv_tutari, s.id, 'SATIS', f'{f.fatura_no} KDV')
        audit('SatisFaturalari', f.id, 'CREATE', '03_SEVK', '04_FATURALAND', f'{f.fatura_no} oluşturuldu')
        db.session.commit(); flash(f'{f.fatura_no} oluşturuldu, sipariş Faturalandı (04).','success')
    except Exception as e:
        db.session.rollback(); flash(f'ROLLBACK: {str(e)}','danger')
        return redirect(url_for('siparis_detay', id=id))
    return redirect(url_for('satis_fatura_detay', id=s.fatura.id))

@app.route('/siparis/<int:id>/iptal', methods=['POST'])
def siparis_iptal(id):
    s = SatisSimdi.query.get_or_404(id)
    if s.durum in ['04_FATURALAND']:
        flash('Faturası kesilmiş sipariş iptal edilemez.','danger'); return redirect(url_for('siparis_detay', id=id))
    try:
        eski = s.durum
        if s.durum == '02_PICKING':
            for k in s.kalemler:
                stok_lot_ekle(k.urun, k.miktar, float(k.maliyet or k.urun.ortalama_maliyet), f'İptal: {s.siparis_no}')
                db.session.add(StokHareketi(urun_id=k.urun_id, tur='Giriş',
                    miktar=k.miktar, aciklama=f'İptal iadesi: {s.siparis_no}'))
                gl_yaz('COGS', 0, k.toplam, s.id, 'SATIS', f'İptal: {s.siparis_no}')
                gl_yaz('Stok_Varlik', float(k.maliyet or 0)*k.miktar, 0, s.id, 'SATIS', f'İptal: {s.siparis_no}')
        s.durum = 'XX_IPTAL'
        audit('SatisSimdi', s.id, 'STATUS_CHANGE', eski, 'XX_IPTAL', 'Sipariş iptal edildi')
        db.session.commit(); flash('İptal edildi, stok iade edildi.','warning')
    except Exception as e:
        db.session.rollback(); flash(f'ROLLBACK: {str(e)}','danger')
    return redirect(url_for('siparis_detay', id=id))

# ════════════════════════════════════════════════════════════
#  SATIŞ FATURA
# ════════════════════════════════════════════════════════════

@app.route('/faturalar')
def faturalar():
    durum = request.args.get('durum','')
    q = SatisFaturasi.query.order_by(SatisFaturasi.tarih.desc())
    if durum: q = q.filter_by(odeme_durumu=durum)
    return render_template('faturalar.html', faturalar=q.all(), durum=durum)

@app.route('/fatura/<int:id>')
def satis_fatura_detay(id):
    return render_template('fatura_detay.html', fatura=SatisFaturasi.query.get_or_404(id))

@app.route('/fatura/<int:id>/odendi', methods=['POST'])
def fatura_odendi(id):
    f = SatisFaturasi.query.get_or_404(id)
    try:
        f.odeme_durumu = 'Ödendi'
        gl_yaz('Kasa', f.genel_toplam, 0, f.siparis_id, 'SATIS', f'{f.fatura_no} tahsilat')
        gl_yaz('Alacak_Hesaplari', 0, f.genel_toplam, f.siparis_id, 'SATIS', f'{f.fatura_no} kapatıldı')
        audit('SatisFaturalari', f.id, 'PAYMENT', 'Ödenmedi', 'Ödendi')
        db.session.commit(); flash('Tahsilat kaydedildi, GL güncellendi.','success')
    except Exception as e:
        db.session.rollback(); flash(f'ROLLBACK: {str(e)}','danger')
    return redirect(url_for('satis_fatura_detay', id=id))

# ════════════════════════════════════════════════════════════
#  SATIN ALMA — PROCURE-TO-PAY
#  Slide 33: PR → PO → GRN → Vendor Invoice (3-Way Match) → Payment
# ════════════════════════════════════════════════════════════

@app.route('/satin-alma')
def satin_alma():
    durum = request.args.get('durum','')
    q = SatinAlmaSiparisi.query.order_by(SatinAlmaSiparisi.tarih.desc())
    if durum: q = q.filter_by(durum=durum)
    return render_template('satin_alma.html', liste=q.all(), durum=durum, po_states=PO_STATES)

@app.route('/satin-alma/yeni', methods=['GET','POST'])
def satin_alma_yeni():
    if request.method == 'POST':
        po = SatinAlmaSiparisi(po_no=po_no_uret(),
            tedarikci_id=request.form.get('tedarikci_id') or None,
            notlar=request.form.get('notlar',''))
        db.session.add(po); db.session.flush()
        for uid, mik, mal in zip(request.form.getlist('urun_id'),
                                  request.form.getlist('miktar'),
                                  request.form.getlist('birim_maliyet')):
            if uid and mik and int(mik) > 0:
                db.session.add(SatinAlmaKalemi(po_id=po.id, urun_id=int(uid),
                    miktar=int(mik), birim_maliyet=float(mal or 0)))
        audit('SatinAlmaSiparisleri', po.id, 'CREATE', '', 'TASLAK', f'{po.po_no} PR oluşturuldu')
        db.session.commit(); flash(f'{po.po_no} satın alma talebi (PR) oluşturuldu.','success')
        return redirect(url_for('satin_alma_detay', id=po.id))
    return render_template('satin_alma_form.html',
        tedarikciler=Tedarikci.query.filter_by(is_active=True).all(),
        urunler=Urun.query.filter_by(is_active=True).all())

@app.route('/satin-alma/<int:id>')
def satin_alma_detay(id):
    return render_template('satin_alma_detay.html',
        po=SatinAlmaSiparisi.query.get_or_404(id), po_states=PO_STATES)

@app.route('/satin-alma/<int:id>/onayla', methods=['POST'])
def satin_alma_onayla(id):
    # Slide 37: PR → PO conversion
    po = SatinAlmaSiparisi.query.get_or_404(id)
    eski = po.durum; po.durum = 'ONAYLANDI'
    audit('SatinAlmaSiparisleri', po.id, 'STATUS_CHANGE', eski, 'ONAYLANDI', 'PR → PO dönüşümü')
    db.session.commit(); flash(f'{po.po_no} PO olarak onaylandı.','success')
    return redirect(url_for('satin_alma_detay', id=id))

@app.route('/satin-alma/<int:id>/gonder', methods=['POST'])
def satin_alma_gonder(id):
    po = SatinAlmaSiparisi.query.get_or_404(id)
    eski = po.durum; po.durum = 'GONDERILDI'
    audit('SatinAlmaSiparisleri', po.id, 'STATUS_CHANGE', eski, 'GONDERILDI')
    db.session.commit(); flash(f'{po.po_no} tedarikçiye gönderildi.','success')
    return redirect(url_for('satin_alma_detay', id=id))

@app.route('/satin-alma/<int:id>/grn', methods=['POST'])
def satin_alma_grn(id):
    import random
    po = SatinAlmaSiparisi.query.get_or_404(id)
    try:
        for k in po.kalemler:
            stok_lot_ekle(k.urun, k.miktar, k.birim_maliyet, po.po_no)
            k.teslim_miktar = k.miktar
            db.session.add(StokHareketi(urun_id=k.urun_id, tur='Giriş',
                miktar=k.miktar, aciklama=f'GRN: {po.po_no}'))
            gl_yaz('Stok_Varlik', float(k.birim_maliyet)*k.miktar, 0, po.id, 'PO', f'GRN: {po.po_no}')
            gl_yaz('GR_IR_Karsilastirma', 0, float(k.birim_maliyet)*k.miktar, po.id, 'PO', f'GRN: {po.po_no}')
        po.grn_tarihi = datetime.utcnow()

        # Otomatik Vendor Invoice - rastgele +/-%3 fark
        po_toplam = po.toplam
        fark_yuzdesi = random.uniform(-0.03, 0.03)
        invoice_tutar = round(po_toplam * (1 + fark_yuzdesi), 2)
        invoice_no = f"VI-{po.po_no}-{datetime.utcnow().strftime('%Y%m%d')}"
        tolerans = 0.05
        fark_oran = abs(invoice_tutar - po_toplam) / po_toplam if po_toplam > 0 else 0
        match_durumu = 'Eslesti' if fark_oran <= tolerans else 'Bloklandı'

        vi = VendorInvoice(
            po_id=po.id,
            invoice_no=invoice_no,
            toplam_tutar=invoice_tutar,
            match_durumu=match_durumu,
            notlar=f'Otomatik | PO: {po_toplam:.2f} TL | Fatura: {invoice_tutar:.2f} TL | Fark: %{fark_oran*100:.2f}'
        )
        db.session.add(vi)

        if match_durumu == 'Eslesti':
            po.durum = 'FATURA_ESLESTI'
            gl_yaz('GR_IR_Karsilastirma', po_toplam, 0, po.id, 'PO', f'3-Way Match: {po.po_no}')
            gl_yaz('Borc_Hesaplari', 0, invoice_tutar, po.id, 'PO', f'AP: {po.po_no}')
            flash(f'{po.po_no} GRN tamamlandi + Vendor Invoice otomatik olusturuldu ({invoice_no}) + 3-Way Match basarili (Fark: %{fark_oran*100:.2f})', 'success')
        else:
            po.durum = 'TESLIM_ALINDI'
            flash(f'{po.po_no} GRN tamamlandi ama 3-Way Match BASARISIZ — Fark %{fark_oran*100:.2f} > %5 — Fatura bloklandi!', 'warning')

        audit('SatinAlmaSiparisleri', po.id, 'GRN_AUTO_INVOICE', 'GONDERILDI', match_durumu, f'Invoice: {invoice_no}')
        db.session.commit()
    except Exception as e:
        db.session.rollback(); flash(f'ROLLBACK: {str(e)}','danger')
    return redirect(url_for('satin_alma_detay', id=id))


@app.route('/satin-alma/<int:id>/vendor-invoice', methods=['POST'])
def vendor_invoice_ekle(id):
    # Slide 42: 3-Way Match — PO Price + GRN Qty + Vendor Invoice
    po = SatinAlmaSiparisi.query.get_or_404(id)
    if po.vendor_invoice:
        flash('Bu PO için zaten fatura girilmiş.','warning'); return redirect(url_for('satin_alma_detay', id=id))
    try:
        invoice_tutar = float(request.form['invoice_tutar'])
        po_tutar = po.toplam
        tolerans = 0.05  # %5 tolerance (Slide 43)
        fark_oran = abs(invoice_tutar - po_tutar) / po_tutar if po_tutar > 0 else 0
        match_durumu = 'Eşleşti' if fark_oran <= tolerans else 'Bloklandı'
        vi = VendorInvoice(po_id=id, invoice_no=request.form.get('invoice_no',''),
            toplam_tutar=invoice_tutar, match_durumu=match_durumu,
            notlar=f'PO tutar: {po_tutar:.2f}, Fatura: {invoice_tutar:.2f}, Fark: %{fark_oran*100:.1f}')
        db.session.add(vi)
        if match_durumu == 'Eşleşti':
            po.durum = 'FATURA_ESLESTI'
            # Slide 43: Clear GR/IR Account
            gl_yaz('GR_IR_Karsilastirma', po_tutar, 0, po.id, 'PO', f'3-Way Match: {po.po_no}')
            gl_yaz('Borc_Hesaplari', 0, invoice_tutar, po.id, 'PO', f'AP: {po.po_no}')
            flash(f'3-Way Match başarılı ✓ ({po.po_no}). Fatura onaylandı.','success')
        else:
            flash(f'3-Way Match başarısız ✗ — Fark %{fark_oran*100:.1f} > %5. Fatura bloklandı!','danger')
        audit('SatinAlmaSiparisleri', po.id, '3WAY_MATCH', po.durum, match_durumu)
        db.session.commit()
    except Exception as e:
        db.session.rollback(); flash(f'ROLLBACK: {str(e)}','danger')
    return redirect(url_for('satin_alma_detay', id=id))

@app.route('/satin-alma/<int:id>/odendi', methods=['POST'])
def satin_alma_odendi(id):
    po = SatinAlmaSiparisi.query.get_or_404(id)
    try:
        po.durum = 'ODENDI'
        if po.vendor_invoice:
            gl_yaz('Borc_Hesaplari', po.vendor_invoice.toplam_tutar, 0, po.id, 'PO', f'Ödeme: {po.po_no}')
            gl_yaz('Kasa', 0, po.vendor_invoice.toplam_tutar, po.id, 'PO', f'Ödeme: {po.po_no}')
        audit('SatinAlmaSiparisleri', po.id, 'PAYMENT', 'FATURA_ESLESTI', 'ODENDI')
        db.session.commit(); flash('Tedarikçi ödemesi kaydedildi.','success')
    except Exception as e:
        db.session.rollback(); flash(f'ROLLBACK: {str(e)}','danger')
    return redirect(url_for('satin_alma_detay', id=id))

@app.route('/satin-alma/<int:id>/iptal', methods=['POST'])
def satin_alma_iptal(id):
    po = SatinAlmaSiparisi.query.get_or_404(id)
    po.durum = 'IPTAL'
    audit('SatinAlmaSiparisleri', po.id, 'STATUS_CHANGE', po.durum, 'IPTAL')
    db.session.commit(); flash('PO iptal edildi.','warning')
    return redirect(url_for('satin_alma_detay', id=id))


# ════════════════════════════════════════════════════════════
#  ÜRETİM ROTALARI
# ════════════════════════════════════════════════════════════

def ue_no_uret():
    son = UretimEmri.query.order_by(UretimEmri.id.desc()).first()
    return f"UE{(son.id+1 if son else 1):05d}"

def mrp_hesapla(mamul_id, uretim_miktari, bom):
    """Slide 36: Net Requirement = Gross Requirement - Available Stock"""
    eksikler = []
    for k in bom.kalemler:
        gereken = k.net_miktar * uretim_miktari
        mevcut  = k.hammadde.stok
        net_ihtiyac = gereken - mevcut
        eksikler.append({
            'urun': k.hammadde,
            'gereken': gereken,
            'mevcut': mevcut,
            'eksik': max(0, net_ihtiyac),
            'yeterli': mevcut >= gereken
        })
    return eksikler

# ── BOM ──────────────────────────────────────────────────────

@app.route('/uretim/bom')
def bom_listesi():
    return render_template('bom_listesi.html',
        bomlar=BOM.query.filter_by(aktif=True).all())

@app.route('/uretim/bom/yeni', methods=['GET','POST'])
def bom_yeni():
    if request.method == 'POST':
        bom = BOM(mamul_id=int(request.form['mamul_id']),
                  versiyon=request.form.get('versiyon','1.0'),
                  aciklama=request.form.get('aciklama',''))
        db.session.add(bom); db.session.flush()
        hammadde_ids  = request.form.getlist('hammadde_id')
        miktarlar     = request.form.getlist('miktar')
        fireler       = request.form.getlist('fire_yuzdesi')
        lead_times    = request.form.getlist('lead_time_gun')
        notlar_kalem  = request.form.getlist('notlar_kalem')
        for i, (hid, mik, fire) in enumerate(zip(hammadde_ids, miktarlar, fireler)):
            if hid and mik and float(mik) > 0:
                lt   = int(lead_times[i])   if i < len(lead_times)   and lead_times[i]   else 0
                not_ = notlar_kalem[i]       if i < len(notlar_kalem) and notlar_kalem[i] else ''
                db.session.add(BOMKalemi(bom_id=bom.id,
                    hammadde_id=int(hid),
                    miktar=float(mik),
                    fire_yuzdesi=float(fire or 0),
                    lead_time_gun=lt,
                    notlar=not_))
        audit('BOM', bom.id, 'CREATE', '', bom.mamul.ad)
        db.session.commit()
        flash(f'BOM oluşturuldu — {bom.mamul.ad} v{bom.versiyon}', 'success')
        return redirect(url_for('bom_detay', id=bom.id))
    urunler = Urun.query.filter_by(is_active=True).all()
    return render_template('bom_form.html', urunler=urunler, bom=None)

@app.route('/uretim/bom/<int:id>')
def bom_detay(id):
    bom = BOM.query.get_or_404(id)
    return render_template('bom_detay.html', bom=bom)

@app.route('/uretim/bom/<int:id>/sil', methods=['POST'])
def bom_sil(id):
    bom = BOM.query.get_or_404(id)
    bom.aktif = False
    db.session.commit(); flash('BOM pasife alındı.','warning')
    return redirect(url_for('bom_listesi'))

# ── ÜRETİM EMRİ ──────────────────────────────────────────────

@app.route('/uretim/emirler')
def ue_listesi():
    durum = request.args.get('durum','')
    q = UretimEmri.query.order_by(UretimEmri.olusturma.desc())
    if durum: q = q.filter_by(durum=durum)
    return render_template('ue_listesi.html', emirler=q.all(), durum=durum, ue_states=UE_STATES)

@app.route('/uretim/emir/yeni', methods=['GET','POST'])
def ue_yeni():
    if request.method == 'POST':
        bom_id = int(request.form['bom_id'])
        bom = BOM.query.get_or_404(bom_id)
        miktar = int(request.form['uretim_miktari'])
        # MRP hesapla — eksik hammadde var mı?
        eksikler = mrp_hesapla(bom.mamul_id, miktar, bom)
        eksik_var = any(not e['yeterli'] for e in eksikler)
        ue = UretimEmri(
            ue_no=ue_no_uret(),
            mamul_id=bom.mamul_id,
            bom_id=bom_id,
            uretim_miktari=miktar,
            notlar=request.form.get('notlar',''),
            planlanan_baslangic=datetime.utcnow())
        db.session.add(ue); db.session.flush()
        audit('UretimEmirleri', ue.id, 'CREATE', '', 'TASLAK')
        db.session.commit()
        if eksik_var:
            flash(f'{ue.ue_no} oluşturuldu ⚠️ Bazı hammaddeler eksik — MRP otomatik PO önerisi oluşturuldu!', 'warning')
        else:
            flash(f'{ue.ue_no} oluşturuldu. Tüm hammaddeler mevcut ✅', 'success')
        return redirect(url_for('ue_detay', id=ue.id))
    bomlar = BOM.query.filter_by(aktif=True).all()
    return render_template('ue_form.html', bomlar=bomlar)

@app.route('/uretim/emir/<int:id>')
def ue_detay(id):
    ue = UretimEmri.query.get_or_404(id)
    eksikler = mrp_hesapla(ue.mamul_id, ue.uretim_miktari, ue.bom)
    return render_template('ue_detay.html', ue=ue, eksikler=eksikler, ue_states=UE_STATES)

@app.route('/uretim/emir/<int:id>/planla', methods=['POST'])
def ue_planla(id):
    ue = UretimEmri.query.get_or_404(id)
    eksikler = mrp_hesapla(ue.mamul_id, ue.uretim_miktari, ue.bom)
    eksik_var = any(not e['yeterli'] for e in eksikler)
    if eksik_var:
        # MRP: eksik hammaddeler için otomatik PO oluştur
        po_listesi = []
        for e in eksikler:
            if not e['yeterli']:
                po = SatinAlmaSiparisi(
                    po_no=po_no_uret(),
                    tedarikci_id=e['urun'].tercihli_tedarikci_id,
                    otomatik=True,
                    notlar=f'MRP: {ue.ue_no} için {e["urun"].ad} — Eksik: {e["eksik"]:.0f} {e["urun"].birim}')
                db.session.add(po); db.session.flush()
                db.session.add(SatinAlmaKalemi(
                    po_id=po.id, urun_id=e['urun'].id,
                    miktar=int(e['eksik']) + e['urun'].yenileme_miktari,
                    birim_maliyet=e['urun'].ortalama_maliyet or e['urun'].satis_fiyati * 0.7))
                po_listesi.append(po.po_no)
        ue.durum = 'PLANLI'
        audit('UretimEmirleri', ue.id, 'STATUS_CHANGE', 'TASLAK', 'PLANLI', f'MRP PO: {", ".join(po_listesi)}')
        db.session.commit()
        flash(f'{ue.ue_no} planlandı. MRP: {len(po_listesi)} PO otomatik oluşturuldu: {", ".join(po_listesi)}', 'warning')
    else:
        ue.durum = 'PLANLI'
        audit('UretimEmirleri', ue.id, 'STATUS_CHANGE', 'TASLAK', 'PLANLI')
        db.session.commit()
        flash(f'{ue.ue_no} planlandı. Tüm hammaddeler mevcut, üretime geçilebilir.', 'success')
    return redirect(url_for('ue_detay', id=id))

@app.route('/uretim/emir/<int:id>/baslat', methods=['POST'])
def ue_baslat(id):
    ue = UretimEmri.query.get_or_404(id)
    # Stok kontrolü
    eksikler = mrp_hesapla(ue.mamul_id, ue.uretim_miktari, ue.bom)
    eksik_var = any(not e['yeterli'] for e in eksikler)
    if eksik_var:
        eksik_adlar = [e['urun'].ad for e in eksikler if not e['yeterli']]
        flash(f'Yetersiz hammadde: {", ".join(eksik_adlar)} -- Once POlari teslim alin!', 'danger')
        return redirect(url_for('ue_detay', id=id))
    try:
        # ACID: tüm hammaddeleri stoktan düş
        for k in ue.bom.kalemler:
            gereken = k.net_miktar * ue.uretim_miktari
            maliyet = stok_lot_dus(k.hammadde, int(gereken))
            db.session.add(UEMalzemeHareketi(
                ue_id=ue.id, hammadde_id=k.hammadde_id,
                miktar=gereken, birim_maliyet=maliyet))
            db.session.add(StokHareketi(urun_id=k.hammadde_id, tur='Çıkış',
                miktar=int(gereken), aciklama=f'Üretim: {ue.ue_no}'))
            # GL: WIP Debit + Hammadde Stok Credit
            gl_yaz('WIP_Uretim', float(maliyet)*gereken, 0, ue.id, 'URETIM', f'{ue.ue_no} hammadde')
            gl_yaz('Stok_Varlik', 0, float(maliyet)*gereken, ue.id, 'URETIM', f'{ue.ue_no} hammadde')
        ue.durum = 'URETIMDE'
        ue.gercek_baslangic = datetime.utcnow()
        audit('UretimEmirleri', ue.id, 'STATUS_CHANGE', 'PLANLI', 'URETIMDE', 'Hammaddeler tüketildi')
        db.session.commit()
        flash(f'{ue.ue_no} üretime başlandı. Hammaddeler stoktan düşüldü, WIP GL yazıldı.', 'success')
    except Exception as e:
        db.session.rollback(); flash(f'ROLLBACK: {str(e)}', 'danger')
    return redirect(url_for('ue_detay', id=id))

@app.route('/uretim/emir/<int:id>/tamamla', methods=['POST'])
def ue_tamamla(id):
    ue = UretimEmri.query.get_or_404(id)
    try:
        # Üretilen mamulü stoğa ekle
        toplam_maliyet = ue.toplam_maliyet
        birim_maliyet  = toplam_maliyet / ue.uretim_miktari if ue.uretim_miktari > 0 else 0
        stok_lot_ekle(ue.mamul, ue.uretim_miktari, birim_maliyet, ue.ue_no)
        db.session.add(StokHareketi(urun_id=ue.mamul_id, tur='Giriş',
            miktar=ue.uretim_miktari, aciklama=f'Üretim tamamlandı: {ue.ue_no}'))
        # GL: Mamul Stok Debit + WIP Credit (COGS transfer)
        gl_yaz('Stok_Varlik', toplam_maliyet, 0, ue.id, 'URETIM', f'{ue.ue_no} mamul stoğa')
        gl_yaz('WIP_Uretim', 0, toplam_maliyet, ue.id, 'URETIM', f'{ue.ue_no} WIP kapatıldı')
        ue.durum = 'TAMAMLANDI'
        ue.gercek_bitis = datetime.utcnow()
        audit('UretimEmirleri', ue.id, 'STATUS_CHANGE', 'URETIMDE', 'TAMAMLANDI',
              f'Birim maliyet: {birim_maliyet:.2f} TL')
        db.session.commit()
        flash(f'{ue.ue_no} tamamlandı! {ue.uretim_miktari} adet {ue.mamul.ad} stoğa eklendi. Birim maliyet: {birim_maliyet:.2f} TL', 'success')
    except Exception as e:
        db.session.rollback(); flash(f'ROLLBACK: {str(e)}', 'danger')
    return redirect(url_for('ue_detay', id=id))

@app.route('/uretim/emir/<int:id>/iptal', methods=['POST'])
def ue_iptal(id):
    ue = UretimEmri.query.get_or_404(id)
    if ue.durum == 'URETIMDE':
        # Hammaddeleri iade et
        for h in ue.malzeme_hareketleri:
            stok_lot_ekle(h.hammadde, int(h.miktar), h.birim_maliyet, f'İptal: {ue.ue_no}')
            db.session.add(StokHareketi(urun_id=h.hammadde_id, tur='Giriş',
                miktar=int(h.miktar), aciklama=f'Üretim iptali: {ue.ue_no}'))
    ue.durum = 'IPTAL'
    audit('UretimEmirleri', ue.id, 'STATUS_CHANGE', ue.durum, 'IPTAL')
    db.session.commit(); flash(f'{ue.ue_no} iptal edildi.', 'warning')
    return redirect(url_for('ue_detay', id=id))

@app.route('/uretim/mrp-analiz')
def mrp_analiz():
    # Tüm aktif BOM'lar için MRP analizi
    bomlar = BOM.query.filter_by(aktif=True).all()
    analiz = []
    for bom in bomlar:
        ihtiyaclar = mrp_hesapla(bom.mamul_id, 1, bom)  # 1 birim için
        analiz.append({'bom': bom, 'ihtiyaclar': ihtiyaclar})
    return render_template('mrp_analiz.html', analiz=analiz)

# ════════════════════════════════════════════════════════════
#  GENEL DEFTER / AUDIT LOG
# ════════════════════════════════════════════════════════════

@app.route('/genel-defter')
def genel_defter():
    return render_template('genel_defter.html',
        kayitlar=GenelDefter.query.order_by(GenelDefter.tarih.desc()).limit(100).all())

@app.route('/audit-log')
def audit_log():
    return render_template('audit_log.html',
        kayitlar=AuditLog.query.order_by(AuditLog.tarih.desc()).limit(100).all())

@app.route('/stok-hareketleri')
def stok_hareketleri():
    return render_template('stok_hareketleri.html',
        hareketler=StokHareketi.query.order_by(StokHareketi.tarih.desc()).limit(100).all())

# Upload route
@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# API
@app.route('/api/urun/<int:id>')
def api_urun(id):
    u = Urun.query.get_or_404(id)
    return jsonify({'fiyat': float(u.satis_fiyati), 'stok': u.stok,
                    'birim': u.birim, 'maliyet': u.mevcut_maliyet})

# ════════════════════════════════════════════════════════════
#  BAŞLAT + ÖRNEK VERİ
# ════════════════════════════════════════════════════════════

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        if Kategori.query.count() == 0:
            for k in ['Elektronik','Aksesuar','Yazıcı & Sarf','Ağ Ekipmanı']:
                db.session.add(Kategori(ad=k))
            t1 = Tedarikci(ad='TeknoTedarik A.Ş.', telefon='0212 111 22 33',
                email='info@teknotedarik.com', adres='İstanbul', vergi_no='1111111111')
            t2 = Tedarikci(ad='BilişimPark Ltd.', telefon='0312 222 33 44',
                email='satis@bilisimpark.com', adres='Ankara', vergi_no='2222222222')
            db.session.add_all([t1, t2])
            m1 = Musteri(ad='ABC Toptan Ltd.', telefon='0312 111 22 33',
                email='info@abc.com', adres='Ankara', vergi_no='1234567890', kredi_limiti=100000)
            m2 = Musteri(ad='XYZ Dağıtım A.Ş.', telefon='0216 444 55 66',
                email='satis@xyz.com', adres='İstanbul', vergi_no='9876543210', kredi_limiti=75000)
            db.session.add_all([m1, m2])
            db.session.flush()
            urunler = [
                Urun(ad='Laptop Pro X', sku='LP-001', kategori_id=1, satis_fiyati=28000,
                     minimum_stok=5, yenileme_miktari=20, maliyet_yontemi='FIFO',
                     tercihli_tedarikci_id=t1.id, temin_suresi_gun=3),
                Urun(ad='Monitör 27" 4K', sku='MN-001', kategori_id=1, satis_fiyati=9500,
                     minimum_stok=5, yenileme_miktari=15, maliyet_yontemi='FIFO',
                     tercihli_tedarikci_id=t1.id),
                Urun(ad='Mekanik Klavye', sku='KB-001', kategori_id=2, satis_fiyati=1400,
                     minimum_stok=10, yenileme_miktari=50, maliyet_yontemi='AVERAGE',
                     tercihli_tedarikci_id=t2.id),
                Urun(ad='Kablosuz Mouse', sku='MS-001', kategori_id=2, satis_fiyati=750,
                     minimum_stok=10, yenileme_miktari=50, maliyet_yontemi='AVERAGE',
                     tercihli_tedarikci_id=t2.id),
                Urun(ad='Toner XL', sku='TN-001', kategori_id=3, satis_fiyati=520,
                     minimum_stok=15, yenileme_miktari=100, maliyet_yontemi='LIFO',
                     tercihli_tedarikci_id=t2.id),
            ]
            db.session.add_all(urunler); db.session.flush()
            # Başlangıç lotları — FIFO demo: 2 farklı fiyatta lot
            stok_lot_ekle(urunler[0], 30, 19000, 'Başlangıç Lot-1')
            stok_lot_ekle(urunler[0], 20, 20500, 'Başlangıç Lot-2')
            stok_lot_ekle(urunler[1], 25, 6800, 'Başlangıç')
            stok_lot_ekle(urunler[2], 8,  900, 'Başlangıç')    # kritik stok demo
            stok_lot_ekle(urunler[3], 4,  480, 'Başlangıç')    # kritik stok demo
            stok_lot_ekle(urunler[4], 40, 320, 'Başlangıç')
            db.session.commit()
    app.run(debug=True, port=5050)
