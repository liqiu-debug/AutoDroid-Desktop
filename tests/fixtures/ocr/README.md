`refund.png` is a synthetic screenshot with two lines: `退款金额 123.45` and
`Order AB123`. It verifies Chinese/English OCR, decimal extraction, bounding
boxes, and selecting the upper half of a screenshot. The PNG is committed so
the tests do not depend on an installed Chinese font or a connected device.
