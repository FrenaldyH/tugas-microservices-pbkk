# Sistem Microservices E-Commerce Sederhana — Tugas PBKK

Sistem terdiri atas tiga service domain, satu API Gateway, dan satu service
discovery, dijalankan bersama lewat Docker Compose. Setiap service punya
database sendiri dan tidak pernah menyentuh database milik service lain.

## Arsitektur

```
                    ┌──────────────────┐        ┌──────────────┐
   Browser ────────▶│   API Gateway    │───────▶│    Consul    │
   (dashboard)      │      :8000       │ lookup │    :8500     │
                    └────────┬─────────┘        └──────▲───────┘
                             │ HTTP                    │ register + health check
              ┌──────────────┼──────────────┐          │
              ▼              ▼              ▼          │
      ┌──────────────┐ ┌─────────────┐ ┌─────────────┐ │
      │ user-service │ │  product-   │ │   order-    │─┘
      │    :8001     │ │  service    │ │   service   │
      │              │ │   :8002     │ │    :8003    │
      └──────┬───────┘ └──────┬──────┘ └──────┬──────┘
             │                │               │
        ┌────▼────┐      ┌────▼─────┐    ┌────▼────┐
        │ user-db │      │product-db│    │order-db │      semua: PostgreSQL 16
        └─────────┘      └──────────┘    └─────────┘

      order-service memanggil user-service & product-service
      (alamatnya selalu dari Consul, tidak pernah hardcode)
```

## Menjalankan

Prasyarat: Docker Engine + plugin Compose. Di Ubuntu:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER   # lalu logout/login, atau: newgrp docker
```

Jalankan seluruh sistem:

```bash
docker compose up -d --build     # pertama kali ± 2-4 menit (build image)
docker compose ps                # semua harus "healthy"
```

Verifikasi otomatis (hanya butuh Python bawaan, tanpa pip install):

```bash
python3 tests/e2e.py
```

Menghentikan:

```bash
docker compose down        # hentikan, data tetap ada
docker compose down -v     # hentikan sekaligus hapus isi database
```

## Alamat yang bisa dibuka


| URL                                                | Isi                                                |
| ---------------------------------------------------- | ---------------------------------------------------- |
| http://localhost:8000                              | Dashboard demo (dilayani Gateway)                  |
| http://localhost:8000/docs                         | Swagger UI milik Gateway                           |
| http://localhost:8500                              | Consul UI — daftar service & status health, live  |
| http://localhost:16686                             | Jaeger UI — alur satu request antar service       |
| http://localhost:8001/docs, :8002/docs, :8003/docs | Swagger tiap service (khusus debugging)            |
| localhost:5433 / 5434 / 5435                       | Postgres user / product / order (khusus debugging) |

Port tiap service dan tiap database sengaja dibuka ke host **hanya untuk
keperluan pemeriksaan saat belajar**. Di lingkungan sungguhan hanya port 8000
yang dibuka, sisanya cukup hidup di jaringan internal Docker.

## Alur demo yang disarankan

1. Buka http://localhost:8000. Panel atas menampilkan status keempat service,
   diambil langsung dari Consul setiap 3 detik.
2. Jalankan langkah 1 → 2 → 3 → 4 di halaman (daftar user, login, tambah
   produk, buat order). Panel log memperlihatkan request dan response mentah.
3. Buka Jaeger, pilih service `gateway`, lalu buka trace `POST /api/{path}`
   terakhir. Terlihat satu request memecah jadi rangkaian span:
   Gateway → order-service → user-service dan product-service.
4. Tunjukkan service discovery bekerja:

   - `docker compose kill product-service` — ini mensimulasikan service mati
     mendadak. Setelah ±20 detik, health check Consul gagal dan statusnya jadi
     `critical` (titik merah di dashboard), lalu `GET /api/products` dijawab
     `503` oleh Gateway.
   - `docker compose start product-service` — status kembali hijau sendiri.
   - Catatan: `docker compose stop` berbeda — service sempat berpamitan
     (deregistrasi) sehingga namanya hilang dari daftar, bukan berubah merah.
5. Tunjukkan kompensasi stok: buat order dua item, item kedua dengan jumlah
   melebihi stok. Order ditolak `409`, dan stok item pertama tidak berkurang
   karena sudah dikembalikan.
6. Tunjukkan isolasi database:

   ```bash
   docker compose exec user-db psql -U user_svc -d user_db -c '\dt'
   ```

   Hanya ada tabel `users`; tabel `products` dan `orders` ada di database lain.

## Endpoint

Semua diakses lewat Gateway dengan awalan `/api`.


| Method | Path                         | Service | Keterangan                                   |
| -------- | ------------------------------ | --------- | ---------------------------------------------- |
| POST   | `/api/users`                 | user    | Daftar user baru                             |
| POST   | `/api/auth/login`            | user    | Login, mengembalikan JWT                     |
| GET    | `/api/users/{id}`            | user    | Detail user                                  |
| GET    | `/api/products`              | product | Katalog                                      |
| POST   | `/api/products`              | product | Tambah produk                                |
| GET    | `/api/products/{id}`         | product | Detail produk                                |
| POST   | `/api/products/{id}/reserve` | product | Kurangi stok (dipakai order-service)         |
| POST   | `/api/products/{id}/release` | product | Kembalikan stok (kompensasi)                 |
| POST   | `/api/orders`                | order   | Buat order —**butuh token**                 |
| GET    | `/api/orders/{id}`           | order   | Detail order milik sendiri —**butuh token** |

Selain itu Gateway menyediakan `GET /registry` (ringkasan status dari Consul,
dipakai dashboard) dan `GET /health`.

## Pola microservices yang didemonstrasikan


| Pola                        | Wujudnya di kode                                                                    |
| ----------------------------- | ------------------------------------------------------------------------------------- |
| API Gateway                 | `gateway/main.py` — satu pintu masuk, routing berdasarkan awalan path              |
| Service Discovery           | `common/platform.py` — `register()` saat start, `discover()` saat memanggil        |
| Health check                | Tiap service punya`/health` yang menyentuh database, dipanggil Consul tiap 10 detik |
| Database per service        | Tiga container Postgres terpisah, tiga volume terpisah                              |
| Autentikasi terpusat        | Gateway memverifikasi JWT sekali, lalu meneruskan`X-User-Id`                        |
| Komunikasi antar service    | `services/order-service/main.py` memanggil dua service lain lewat HTTP              |
| Kompensasi (saga sederhana) | `release_all()` mengembalikan stok saat order gagal di tengah                       |
| Distributed tracing         | OpenTelemetry auto-instrument, dikirim ke Jaeger                                    |

### Catatan tentang keamanan

Gateway adalah batas kepercayaan (*trust boundary*). Ia memverifikasi token,
lalu membuang header `X-User-Id` bawaan client dan menggantinya dengan
identitas hasil verifikasi — sehingga client tidak bisa mengaku sebagai user
lain. Service internal mempercayai header itu karena hanya bisa dicapai dari
jaringan internal Docker. `GET /api/orders/{id}` juga memeriksa kepemilikan:
order milik user lain dijawab `404`, bukan `403`, supaya tidak membocorkan
order mana saja yang ada.

## Keputusan desain

- **Consul, bukan Eureka.** Eureka secara desain adalah komponen Java
  (Netflix); memakainya dari Python butuh library pihak ketiga yang kurang
  terawat. Consul bahasa-agnostik, cukup HTTP API biasa, dan punya UI bawaan
  yang berguna untuk demo.
- **Gateway ditulis sendiri, bukan Traefik/Kong.** Tujuan tugas ini memahami
  cara kerja gateway, jadi routing dan lookup-nya dibuat eksplisit dan bisa
  dibaca (± 130 baris), bukan disembunyikan di balik konfigurasi.
- **Lookup Consul dilakukan tiap request, tanpa cache.** Sedikit lebih lambat,
  tapi tidak ada alamat basi dan efeknya langsung terlihat ketika sebuah
  service dimatikan.
- **Harga disimpan sebagai bilangan bulat rupiah.** Rupiah tidak punya satuan
  pecahan yang dipakai sehari-hari, sehingga integer membuat perhitungan uang
  tepat tanpa masalah pembulatan angka desimal.
- **Pengurangan stok memakai satu perintah `UPDATE ... WHERE stock >= :qty`.**
  Kalau stok dibaca dulu lalu ditulis belakangan, dua order bersamaan bisa
  sama-sama lolos pemeriksaan dan stok jadi minus. Satu statement membuat
  pemeriksaan dan pengurangan terjadi sekaligus di database.
- **`common/platform.py` dipakai bersama.** Registrasi Consul dan setup tracing
  identik di semua service; menyalinnya empat kali hanya menambah tempat untuk
  lupa memperbarui. Konsekuensinya keempat komponen berbagi satu build context
  — masih wajar untuk skala tugas, dan lazim disebut *platform library*.

## Batasan yang diketahui

Ditandai juga di kode dengan komentar `ponytail:`.

- Komunikasi seluruhnya sinkron (REST). Belum ada message queue / event-driven.
- Kompensasi stok bersifat *best-effort*: bila panggilan pengembalian stok ikut
  gagal, kejadian itu hanya dicatat di log, belum ada antrian percobaan ulang.
- `discover()` selalu mengambil instance pertama — belum ada load balancing
  bila satu service dijalankan lebih dari satu replika.
- Kredensial database dan `JWT_SECRET` ditulis apa adanya di
  `docker-compose.yml`; ini khusus untuk pengembangan lokal.
- Katalog produk belum berpaginasi.

## Struktur folder

```
.
├── docker-compose.yml          # definisi 9 container
├── common/platform.py          # registrasi Consul, discovery, tracing
├── gateway/
│   ├── main.py                 # proxy + verifikasi JWT + /registry
│   └── static/index.html       # dashboard demo
├── services/
│   ├── user-service/main.py    # registrasi, login, detail user
│   ├── product-service/main.py # katalog + reserve/release stok
│   └── order-service/main.py   # order lintas service + kompensasi
├── tests/e2e.py                # pengujian end-to-end lewat Gateway
└── PLAN.md                     # rencana awal beserta alasan pemilihannya
```
