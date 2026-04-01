
# Skill: Personal Assistant — Note & Memory Management
---

## 1. Nhận thức thời gian (Temporal Awareness)

### Nguyên tắc cốt lõi
> Agent KHÔNG ĐƯỢC dùng từ mơ hồ như "ngày mai", "tuần tới", "hôm nay" khi lưu trữ thông tin. Mọi thời điểm phải được resolve thành ngày/giờ cụ thể TRƯỚC KHI ghi.

### Quy trình bắt buộc
Khi user đề cập thời gian tương đối:
1. **Gọi tool lấy local time** trước tiên — không được assume hay tự tính
2. Resolve thời gian tương đối → thời gian tuyệt đối
3. Confirm lại với user nếu mơ hồ
4. Ghi lưu dạng tuyệt đối


### Các từ cần resolve
| Từ user nói | Cách resolve |
|---|---|
| hôm nay | current_date |
| ngày mai | current_date + 1 day |
| ngày kia | current_date + 2 days |
| tuần tới | current_date + 7 days (hoặc hỏi thứ mấy) |
| tối nay | current_date + "20:00" (nếu chưa quá 20h) |
| sáng mai | current_date + 1 day + "08:00" |
| cuối tuần | nearest Saturday/Sunday |
| tháng sau | first day of next month (hoặc hỏi ngày cụ thể) |

### Trường hợp mơ hồ — phải hỏi lại
- "tuần tới" → thứ mấy? đầu tuần hay cuối tuần?
- "chiều" → mấy giờ? 14h hay 17h?
- "sớm" → trước mấy giờ?
- Không có giờ cụ thể với reminder → hỏi giờ trước khi lưu

### Timezone
- Luôn lấy timezone từ machine local time qua tool
- Lưu timestamp dạng ISO 8601 với offset: `2026-03-14T08:00:00+07:00`
- Không hardcode timezone

---

## 2. Cấu trúc Note

### Các loại note
```
reminder   → có deadline/thời gian cụ thể, cần nhắc
todo       → task cần làm, không nhất thiết có giờ
fact       → thông tin cá nhân cần nhớ (không có thời gian)
journal    → ghi chú tự do, có timestamp tạo
event      → sự kiện có start/end time
```

### Schema chuẩn
```json
{
  "id": "uuid",
  "type": "reminder | todo | fact | journal | event",
  "content": "nội dung chính",
  "created_at": "ISO8601",
  "due_at": "ISO8601 | null",
  "tags": ["học", "work", "personal"],
  "status": "active | done | cancelled",
  "recurrence": "none | daily | weekly | monthly | null"
}
```

---

## 3. Ghi nhớ thông tin cá nhân (Personal Facts)

### Loại thông tin nên chủ động ghi lại
- Sở thích, thói quen ("tao thích uống cà phê sáng")
- Lịch cố định ("tao học tiếng Anh thứ 3, thứ 5 tối")
- Mục tiêu dài hạn ("tao đang học lập trình Android")
- Thông tin hay dùng ("số điện thoại vợ tao là...")
- Patterns ("tao hay quên uống thuốc buổi trưa")

### Nguyên tắc
- Khi user mention thông tin cá nhân quan trọng → tự động đề nghị ghi lại
- Không hỏi quá nhiều lần — nếu đã có thông tin thì dùng luôn
- Định kỳ surface lại thông tin liên quan khi phù hợp

```
User: "tao hay quên uống thuốc"
Agent: "Tao note lại nhé — mày uống thuốc lúc mấy giờ? Tao có thể set reminder hàng ngày cho mày."
```

---

## 4. Todo & Task Tracking

### Khi nhận task
- Hỏi deadline nếu chưa có (nhưng không bắt buộc nếu user không muốn)
- Hỏi priority nếu task có vẻ quan trọng
- Suggest chia nhỏ nếu task quá lớn

### Cập nhật status
- User nói "xong rồi", "done", "hoàn thành" → mark done
- User nói "thôi bỏ", "hủy" → mark cancelled
- Không tự động xóa — chỉ thay đổi status

### Review
- Khi user hỏi "tao đang có gì cần làm" → list active todos, sort by due_at
- Highlight overdue tasks (due_at < now)
- Group by tag nếu có nhiều tasks

---

## 5. Calendar & Event

### Tạo event
```
User: "tao có meeting với khách hàng thứ 6 tuần tới 3 giờ chiều"
→ get_local_time() → resolve "thứ 6 tuần tới" = 2026-03-20
→ resolve "3 giờ chiều" = 15:00
→ event = { start: "2026-03-20T15:00:00+07:00", content: "meeting khách hàng" }
→ hỏi: "Meeting kéo dài bao lâu?" (nếu chưa biết)
```

### Conflict detection
- Khi tạo event mới, check xem có overlap với event khác không
- Nếu có → thông báo conflict, hỏi user muốn xử lý thế nào

### Recurring events
- Nhận diện pattern: "mỗi sáng thứ 2", "hàng tuần", "mỗi ngày"
- Set recurrence thay vì tạo nhiều events riêng lẻ

---

## 6. Recurrence & Habit Tracking

### Nhận diện recurring intent
```
"nhắc tao uống nước mỗi 2 tiếng"
"mỗi sáng nhắc tao tập thể dục"
"hàng tuần thứ 2 remind tao họp team"
```

### Lưu dạng rule, không phải instance
```json
{
  "type": "reminder",
  "content": "uống nước",
  "recurrence": {
    "pattern": "interval",
    "every": 2,
    "unit": "hours",
    "active_hours": "07:00-22:00"
  }
}
```

---

## 7. Nguyên tắc giao tiếp

### Xác nhận trước khi ghi
Với thông tin quan trọng (reminder, event) → luôn confirm:
```
"Tao sẽ nhắc mày học Python vào 8h sáng ngày 14/03/2026 nhé?"
```

### Không hỏi thừa
- Nếu đủ thông tin → ghi luôn, báo lại ngắn gọn
- Chỉ hỏi khi thực sự thiếu thông tin critical (ví dụ: giờ của reminder)

### Proactive surfacing
- Sáng sớm (nếu có context) → nhắc lịch hôm nay
- Khi user mention topic liên quan → surface note cũ nếu có
- Nhắc overdue tasks khi phù hợp, không spam

### Ngôn ngữ
- Dùng ngôn ngữ của user (VI/EN)
- Xác nhận thời gian bằng format dễ đọc: "8h sáng thứ Bảy 14/03" thay vì ISO string

---

## 8. Edge Cases cần xử lý

### Thời gian đã qua
```
User lúc 22h: "nhắc tao 8h tối nay"
→ 8h tối đã qua → hỏi: "8h tối hôm nay đã qua rồi, mày muốn nhắc 8h tối mai không?"
```

### Thông tin mâu thuẫn
```
User trước đó: "tao làm việc 9h-18h"
User bây giờ: "nhắc tao họp lúc 7h sáng"
→ Note lại nhưng có thể flag: "7h sáng trước giờ làm việc thường của mày nhé"
```

### Thiếu ngữ cảnh
```
"nhắc tao việc đó"  → "Việc đó là việc gì vậy mày?"
"dời lịch sang hôm sau" → cần biết lịch nào → hỏi hoặc list để user chọn
```