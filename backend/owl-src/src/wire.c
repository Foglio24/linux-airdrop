/*
 * OWL: an open Apple Wireless Direct Link (AWDL) implementation
 * Copyright (C) 2018  The Open Wireless Link Project (https://owlink.org)
 * Copyright (C) 2018  Milan Stute
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 */

#include <string.h>
#include <stdlib.h>

#include "wire.h"

struct buf {
	const uint8_t *orig;
	uint8_t *data;
	int len;
	int owned;
};

/* Subtraction after checking offset avoids signed overflow for hostile sizes. */
static int buf_contains(const struct buf *buf, int offset, int length) {
	return buf && offset >= 0 && length >= 0 && offset <= buf->len && length <= buf->len - offset;
}

struct buf *buf_new_owned(int len) {
	struct buf *buf;
	if (len < 0)
		return NULL;
	buf = malloc(sizeof(*buf));
	if (!buf)
		return NULL;
	/* Keep even empty owned buffers backed by a valid allocation. */
	buf->data = malloc(len ? (size_t) len : 1);
	if (!buf->data) {
		free(buf);
		return NULL;
	}
	buf->orig = buf->data;
	buf->len = len;
	buf->owned = 1;
	return buf;
}

const struct buf *buf_new_const(const uint8_t *data, int len) {
	struct buf *buf;
	if (len < 0 || (!data && len))
		return NULL;
	buf = malloc(sizeof(*buf));
	if (!buf)
		return NULL;
	buf->data = (uint8_t *) data;
	buf->orig = data;
	buf->len = len;
	buf->owned = 0;
	return buf;
}

void buf_free(const struct buf *buf) {
	if (!buf)
		return;
	if (buf->owned)
		free((void *) buf->orig);
	free((struct buf *) buf);
}

const uint8_t *buf_data(const struct buf *buf) {
	return buf ? buf->data : NULL;
}

int buf_len(const struct buf *buf) {
	return buf ? buf->len : 0;
}

int buf_strip(const struct buf *buf, int len) {
	if (!buf_contains(buf, 0, len))
		return OUT_OF_BOUNDS;
	if (len)
		((struct buf *) buf)->data += len;
	((struct buf *) buf)->len -= len;
	return len;
}

int buf_take(const struct buf *buf, int len) {
	if (!buf_contains(buf, 0, len))
		return OUT_OF_BOUNDS;
	((struct buf *) buf)->len -= len;
	return len;
}

int write_bytes(struct buf *buf, int offset, const uint8_t *bytes, int length) {
	if (!buf_contains(buf, offset, length) || (!bytes && length))
		return OUT_OF_BOUNDS;
	if (length)
		memcpy(buf->data + offset, bytes, (size_t) length);
	return length;
}

int write_u8(struct buf *buf, int offset, uint8_t value) {
	return write_bytes(buf, offset, &value, sizeof(value));
}

int write_le16(struct buf *buf, int offset, uint16_t value) {
	value = htole16(value);
	return write_bytes(buf, offset, (const uint8_t *) &value, sizeof(value));
}

int write_be16(struct buf *buf, int offset, uint16_t value) {
	value = htobe16(value);
	return write_bytes(buf, offset, (const uint8_t *) &value, sizeof(value));
}

int write_le32(struct buf *buf, int offset, uint32_t value) {
	value = htole32(value);
	return write_bytes(buf, offset, (const uint8_t *) &value, sizeof(value));
}

int write_be32(struct buf *buf, int offset, uint32_t value) {
	value = htobe32(value);
	return write_bytes(buf, offset, (const uint8_t *) &value, sizeof(value));
}

int write_ether_addr(struct buf *buf, int offset, const struct ether_addr *addr) {
	return write_bytes(buf, offset, (const uint8_t *) addr, ETHER_ADDR_LEN);
}

int read_bytes(const struct buf *buf, int offset, const uint8_t **bytes, int length) {
	if (!buf_contains(buf, offset, length))
		return OUT_OF_BOUNDS;
	if (bytes)
		*bytes = offset ? buf->data + offset : buf->data;
	return length;
}

int read_bytes_copy(const struct buf *buf, int offset, uint8_t *bytes, int length) {
	if (!buf_contains(buf, offset, length))
		return OUT_OF_BOUNDS;
	if (bytes && length)
		memcpy(bytes, buf->data + offset, (size_t) length);
	return length;
}

int read_u8(const struct buf *buf, int offset, uint8_t *value) {
	return read_bytes_copy(buf, offset, value, sizeof(*value));
}

int read_le16(const struct buf *buf, int offset, uint16_t *value) {
	uint16_t raw;
	int result = read_bytes_copy(buf, offset, (uint8_t *) &raw, sizeof(raw));
	if (result >= 0 && value)
		*value = le16toh(raw);
	return result;
}

int read_be16(const struct buf *buf, int offset, uint16_t *value) {
	uint16_t raw;
	int result = read_bytes_copy(buf, offset, (uint8_t *) &raw, sizeof(raw));
	if (result >= 0 && value)
		*value = be16toh(raw);
	return result;
}

int read_le32(const struct buf *buf, int offset, uint32_t *value) {
	uint32_t raw;
	int result = read_bytes_copy(buf, offset, (uint8_t *) &raw, sizeof(raw));
	if (result >= 0 && value)
		*value = le32toh(raw);
	return result;
}

int read_be32(const struct buf *buf, int offset, uint32_t *value) {
	uint32_t raw;
	int result = read_bytes_copy(buf, offset, (uint8_t *) &raw, sizeof(raw));
	if (result >= 0 && value)
		*value = be32toh(raw);
	return result;
}

int read_ether_addr(const struct buf *buf, int offset, struct ether_addr *addr) {
	return read_bytes_copy(buf, offset, (uint8_t *) addr, ETHER_ADDR_LEN);
}

int read_int_string(const struct buf *buf, int offset, char *str, int length) {
	uint8_t encoded_length;
	int copied_length;
	if (length < 0)
		return OUT_OF_BOUNDS;
	READ_U8(buf, offset, &encoded_length);
	/* Truncating the destination must not accept a truncated wire string. */
	READ_BYTES(buf, offset + 1, NULL, encoded_length);
	copied_length = encoded_length < length ? encoded_length : length;
	READ_BYTES_COPY(buf, offset + 1, (uint8_t *) str, copied_length);
	if (str)
		str[copied_length] = 0;
	return copied_length + 1;
wire_error:
	return OUT_OF_BOUNDS;
}

int read_tlv(const struct buf *buf, int offset, uint8_t *type, uint16_t *len, const uint8_t **val) {
	uint8_t encoded_type;
	uint16_t encoded_length;
	/* Validate the whole header before adding to offset. */
	READ_BYTES(buf, offset, NULL, 3);
	READ_U8(buf, offset, &encoded_type);
	READ_LE16(buf, offset + 1, &encoded_length);
	READ_BYTES(buf, offset + 3, val, encoded_length);
	if (type)
		*type = encoded_type;
	if (len)
		*len = encoded_length;
	return encoded_length + 3;
wire_error:
	return OUT_OF_BOUNDS;
}
