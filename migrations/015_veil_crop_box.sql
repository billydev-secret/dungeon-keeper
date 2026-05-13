-- Migration 015: store the submitter's final crop box so it can be
-- overlaid on the full image when a round is revealed.

ALTER TABLE veil_rounds ADD COLUMN crop_box_x1 REAL;
ALTER TABLE veil_rounds ADD COLUMN crop_box_y1 REAL;
ALTER TABLE veil_rounds ADD COLUMN crop_box_x2 REAL;
ALTER TABLE veil_rounds ADD COLUMN crop_box_y2 REAL;
