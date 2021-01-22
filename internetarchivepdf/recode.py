import pkg_resources

import sys
import os
import subprocess
from os import remove
from time import time
from datetime import datetime
from tempfile import mkstemp
from os.path import join
import shutil
import json
from glob import glob
from math import ceil
from xml.sax.saxutils import escape as xmlescape
import re


from PIL import Image
from PIL import Jpeg2KImagePlugin
from skimage.color import rgb2hsv
import numpy as np
import fitz

from hocr.parse import (hocr_page_iterator, hocr_page_to_word_data,
        hocr_page_get_dimensions)
from internetarchivepdf.mrc import KDU_EXPAND, create_mrc_components, create_mrc_hocr_components, \
        encode_mrc_images
from internetarchivepdf.pdfrenderer import TessPDFRenderer
from internetarchivepdf.pagenumbers import parse_series, series_to_pdf
from internetarchivepdf.scandata import scandata_xml_get_skip_pages, \
        scandata_xml_get_page_numbers, scandata_xml_get_dpi_per_page, \
        scandata_xml_get_document_dpi
from internetarchivepdf.const import (VERSION, SOFTWARE_URL, PRODUCER,
        IMAGE_MODE_PASSTHROUGH, IMAGE_MODE_PIXMAP, IMAGE_MODE_MRC,
        RECODE_RUNTIME_WARNING_INVALID_PAGE_SIZE,
        RECODE_RUNTIME_WARNING_INVALID_PAGE_NUMBERS,)

PDFA_MIN_UNITS = 3
PDFA_MAX_UNITS = 14400


def guess_dpi(w, h, expected_format=(8.27, 11.69), round_to=[72, 96, 150, 300, 600]):
    """
    Guesstimate DPI for a given image.

    Args:

    * w (int): width of the image
    * h (int): height of the image
    * expected_format (tuple): (width_inch, height_inch) of expected document,
                               defaults to european A4.
    * round_to (list of int): List of acceptable DPI values.
                              Defaults to (72, 96, 150, 300, 600)

    Returns an int which is the best matching DPI picked from round_to.
    """
    w_dpi = w / expected_format[0]
    h_dpi = h / expected_format[1]
    diffs = []
    for dpi in round_to:
        diff = abs(w_dpi - dpi) + abs(h_dpi - dpi)
        diffs.append((dpi, diff))
    sorted_diffs = sorted(diffs, key=lambda x: x[1])
    return sorted_diffs[0][0]


perc2val = lambda x: (x*255)/100

def level_arr(arr, minv=0, maxv=255):
    interval = (maxv/255.) - (minv/255.)
    arr_zero = arr < minv
    arr_max = arr > maxv
    arr[::] = ((arr[::] - minv) / interval)
    arr[arr_zero] = 0
    arr[arr_max] = 255
    return arr


# Straight forward port of color2Gray.sh script
# We might be able to do better, but there are only a few users of this script
# in the archive.org currently, so more time has not been invested in finding
# alternative or better ways.
def special_gray_convert(imd):
    components = ('r', 'g', 'b')

    d = {}
    for i, k in enumerate(components):
        for fun in ['min', 'max', 'mean', 'std']:
            d[k + '_' + fun] = getattr(np, fun)(imd[:,:,i]) / 255.

    bright_adjust = round(d['r_mean'] * d['g_mean'] * d['b_mean'] /
                    (d['b_max']*(1-d['r_std'])*(1-d['g_std'])*(1-d['b_std'])), 4)

    low_thres = min(int((196 * d['r_min']+14.5)/1), 50)

    high_thres = {
            'r': min(int((35.66*bright_adjust+48.5)/1), 95),
            'g': min(int((39.22*bright_adjust+44.5)/1), 95),
            'b': min(int((45.16*bright_adjust+36.5)/1), 95),
            }

    new_imd = np.copy(imd)
    for i, c in enumerate(components):
        new_imd[:,:,i] = level_arr(new_imd[:,:,i],
                                   minv=perc2val(low_thres),
                                   maxv=perc2val(high_thres[c]))

    hsv = rgb2hsv(new_imd)
    # Calculate the 'L' from 'HSL' as L = S * (1 - V/2)
    l = hsv[:,:,2] * (1 - (hsv[:,:,1]/2))
    return np.array(l * 255, dtype=np.uint8)


def create_tess_textonly_pdf(hocr_file, save_path, in_pdf=None,
        image_files=None, dpi=None, skip_pages=None, dpi_pages=None,
        reporter=None,
        verbose=False, stop_after=None,
        errors=None):
    hocr_iter = hocr_page_iterator(hocr_file)

    render = TessPDFRenderer()
    render.BeginDocumentHandler()

    skipped_pages = 0

    last_time = time()
    reporting_page_count = 0

    if verbose:
        print('Starting page generation at', datetime.utcnow().isoformat())

    for idx, hocr_page in enumerate(hocr_iter):
        w, h = hocr_page_get_dimensions(hocr_page)

        if skip_pages is not None and idx in skip_pages:
            if verbose:
                print('Skipping page %d' % idx)
            skipped_pages += 1
            continue

        if stop_after is not None and (idx - skipped_pages) >= stop_after:
            break

        if in_pdf is not None:
            page = in_pdf[idx - skipped_pages]
            width = page.rect.width
            height = page.rect.height

            scaler = page.rect.width / w
            ppi = 72 / scaler
        elif image_files is not None:
            # Do not subtract skipped pages here
            imgfile = image_files[idx]

            if imgfile.endswith('.jp2'):
                # Pillow reads the entire file for JPEG2000 images - just to get
                # the image size!
                fd = open(imgfile, 'rb')
                size, mode, mimetype = Jpeg2KImagePlugin._parse_jp2_header(fd)
                fd.close()
                imwidth, imheight = size
            else:
                img = Image.open(imgfile)
                imwidth, imheight = img.size
                del img

            page_dpi = dpi
            per_page_dpi = None

            if dpi_pages is not None:
                try:
                    per_page_dpi = int(dpi_pages[idx - skipped_pages])
                    page_dpi = per_page_dpi
                except:
                    pass  # Keep item-wide dpi if available

            # Both document level dpi is not available and per-page dpi is not
            # available, let's guesstimate
            # Assume european A4 (8.27",11.69") and guess DPI
            # to be one-of (72, 96, 150, 300, 600)
            if page_dpi is None:
                page_dpi = guess_dpi(imwidth, imheight,
                                     expected_format=(8.27, 11.69),
                                     round_to=(72, 96, 150, 300, 600))

            page_width = imwidth / (page_dpi / 72)
            if page_width <= PDFA_MIN_UNITS or page_width >= PDFA_MAX_UNITS:
                if verbose:
                    print('Page size invalid with current image size and dpi.')
                    print('Image size: %d, %d. DPI: %d' % (imwidth, imheight,
                                                           page_dpi))

                # First let's try without per_page_dpi, is avail, then try to
                # guess the page dpi, if that also fails, then set to min
                # or max allowed size
                if per_page_dpi is not None and dpi:
                    if verbose:
                        print('Trying document level dpi:', dpi)
                    page_width = imwidth / (dpi / 72)

                # If that didn't work, guess
                if page_width <= PDFA_MIN_UNITS or page_width >= PDFA_MAX_UNITS:
                    page_dpi = guess_dpi(imwidth, imheight,
                                         expected_format=(8.27, 11.69),
                                         round_to=(72, 96, 150, 300, 600))
                    if verbose:
                        print('Guessing DPI:', dpi)
                    page_width = imwidth / (page_dpi / 72)

                # If even guessing fails, let's hard fail still for now
                if page_width <= PDFA_MIN_UNITS or page_width >= PDFA_MAX_UNITS:
                    raise ValueError('Cannot find a fitting page boundary')

                # Add warning/error
                if errors is not None:
                    errors.add(RECODE_RUNTIME_WARNING_INVALID_PAGE_SIZE)

            scaler = page_width / imwidth

            ppi = 72. / scaler

            width = page_width
            height = imheight * scaler

        word_data = hocr_page_to_word_data(hocr_page, scaler)
        render.AddImageHandler(word_data, width, height, ppi=ppi)

        reporting_page_count += 1


    if verbose:
        print('Finished page generation at', datetime.utcnow().isoformat())
        print('Creating text pages took %.4f seconds' % (time() - last_time))


    if reporter and reporting_page_count != 0:
        current_time = time()
        ms = int(((current_time - last_time) / reporting_page_count) * 1000)

        data = json.dumps({'text_pages': {'count': reporting_page_count,
                                              'time-per': ms}})
        subprocess.check_output(reporter, input=data.encode('utf-8'))

    render.EndDocumentHandler()

    fp = open(save_path, 'wb+')
    fp.write(render._data)
    fp.close()


def get_timing_summary(timing_data):
    sums = {}

    # We expect this to always happen per page
    fg_partial_blur_c = 0

    for v in timing_data:
        key = v[0]
        val = v[1]

        if key == 'fg_partial_blur':
            fg_partial_blur_c += 1

        if key not in sums:
            sums[key] = 0.

        sums[key] += val

    for k in sums.keys():
        sums[k] = sums[k] / fg_partial_blur_c

    for k in sums.keys():
        # For statsd, in ms
        sums[k] = int(sums[k] * 1000)

    return sums



def insert_images_mrc(to_pdf, hocr_file, from_pdf=None, image_files=None,
        bg_slope=None, fg_slope=None,
        skip_pages=None, img_dir=None, jbig2=False, bg_downsample=None,
        denoise_mask=None, reporter=None,
        hq_pages=None, hq_bg_slope=None, hq_fg_slope=None,
        verbose=False, tmp_dir=None, report_every=None,
        stop_after=None, grayscale_pdf=False):
    hocr_iter = hocr_page_iterator(hocr_file)

    skipped_pages = 0

    last_time = time()
    timing_data = []
    reporting_page_count = 0

    #for idx, page in enumerate(to_pdf):
    for idx, hocr_page in enumerate(hocr_iter):
        if skip_pages is not None and idx in skip_pages:
            if verbose:
                print('IMAGES Skipping page %d' % idx)
            skipped_pages += 1
            continue

        idx = idx - skipped_pages

        if stop_after is not None and idx >= stop_after:
            break

        page = to_pdf[idx]

        if from_pdf is not None:
            # XXX: TODO: FIXME: MEGAHACK: For some reason the _imgonly PDFs
            # generated by us have all images on all pages according to pymupdf, so
            # hack around that for now.
            img = sorted(from_pdf.getPageImageList(idx))[idx]
            #img = from_pdf.getPageImageList(idx)[0]

            xref = img[0]
            maskxref = img[1]
            # TODO: Do not assume JPX/JPEG2000 here, probe for image format
            image = from_pdf.extractImage(xref)
            jpx = image["image"]

            fd, jpx_in = mkstemp(prefix='in', suffix='.jpx', dir=tmp_dir)
            os.write(fd, jpx)
            os.close(fd)

            fd, tiff_in = mkstemp(prefix='in', suffix='.tiff', dir=tmp_dir)
            os.close(fd)
            os.remove(tiff_in)

            subprocess.check_call([KDU_EXPAND, '-i', jpx_in, '-o',
                tiff_in], stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL)
            os.remove(jpx_in)

            image = Image.open(tiff_in)
        else:
            # Do not subtract skipped pages here
            imgfile = image_files[idx+skipped_pages]

            if imgfile.endswith('.jp2') or imgfile.endswith('.jpx'):
                fd, tiff_in = mkstemp(prefix='in', suffix='.tiff', dir=tmp_dir)
                os.close(fd)
                os.remove(tiff_in)
                subprocess.check_call([KDU_EXPAND, '-i', imgfile, '-o',
                    tiff_in], stderr=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL)

                image = Image.open(tiff_in)
                image.load()
                os.remove(tiff_in)
            else:
                image = Image.open(imgfile)

        if grayscale_pdf and image.mode not in ('L', 'LA'):
            image = Image.fromarray(special_gray_convert(np.array(image)))

        render_hq = hq_pages[idx]

        hocr_word_data = hocr_page_to_word_data(hocr_page)
        mask, bg, fg = create_mrc_hocr_components(image,
                                                  hocr_word_data,
                                                  bg_downsample=None if render_hq else bg_downsample,
                                                  denoise_mask=denoise_mask,
                                                  timing_data=timing_data)
        if from_pdf is not None:
            remove(tiff_in)

        mask_f, bg_f, fg_f = encode_mrc_images(mask, bg, fg,
                bg_slope=hq_bg_slope if render_hq else bg_slope,
                fg_slope=hq_fg_slope if render_hq else fg_slope,
                tmp_dir=tmp_dir, jbig2=jbig2)

        if img_dir is not None:
            shutil.copy(mask_f, join(img_dir, '%.6d_mask.jbig2' % idx))
            shutil.copy(bg_f, join(img_dir, '%.6d_bg.jp2' % idx))
            shutil.copy(fg_f, join(img_dir, '%.6d_fg.jp2' % idx))

        bg_contents = open(bg_f, 'rb').read()
        page.insertImage(page.rect, stream=bg_contents, mask=None,
                overlay=False)

        fg_contents = open(fg_f, 'rb').read()
        mask_contents = open(mask_f, 'rb').read()

        page.insertImage(page.rect, stream=fg_contents, mask=mask_contents,
                overlay=True)

        # Remove leftover files
        remove(mask_f)
        remove(bg_f)
        remove(fg_f)

        reporting_page_count += 1

        if report_every is not None and reporting_page_count % report_every == 0:
            print('Processed %d PDF pages.' % idx)
            sys.stdout.flush()

            timing_sum = get_timing_summary(timing_data)
            timing_data = []

            if reporter:
                current_time = time()
                ms = int(((current_time - last_time) / reporting_page_count) * 1000)

                data = json.dumps({'compress_pages': {'count': reporting_page_count,
                                                 'time-per': ms},
                                   'page_time_breakdown': timing_sum})
                subprocess.check_output(reporter, input=data.encode('utf-8'))

                # Reset chunk timer
                last_time = time()
                # Reset chunk counter
                reporting_page_count = 0


    if reporter and reporting_page_count != 0:
        current_time = time()
        ms = int(((current_time - last_time) / reporting_page_count) * 1000)

        timing_sum = get_timing_summary(timing_data)

        data = json.dumps({'compress_pages': {'count': reporting_page_count,
                                         'time-per': ms},
                           'page_time_breakdown': timing_sum})
        subprocess.check_output(reporter, input=data.encode('utf-8'))


def insert_images(from_pdf, to_pdf, mode, report_every=None, stop_after=None):
    # TODO: This hasn't been updated, should fix this up, only MRC is tested
    # really.
    # TODO: implement img_dir here

    for idx, page in enumerate(to_pdf):
        # XXX: TODO: FIXME: MEGAHACK: For some reason the _imgonly PDFs
        # generated by us have all images on all pages according to pymupdf, so
        # hack around that for now.
        img = sorted(from_pdf.getPageImageList(idx))[idx]
        #img = from_pdf.getPageImageList(idx)[0]

        xref = img[0]
        maskxref = img[1]
        if mode == IMAGE_MODE_PASSTHROUGH:
            image = from_pdf.extractImage(xref)
            page.insertImage(page.rect, stream=image["image"], overlay=False)
        elif mode == IMAGE_MODE_PIXMAP:
            pixmap = fitz.Pixmap(from_pdf, xref)
            page.insertImage(page.rect, pixmap=pixmap, overlay=False)

        if stop_after is not None and idx >= stop_after:
            break

        if report_every is not None and idx % report_every == 0:
            print('Processed %d PDF pages.' % idx)
            sys.stdout.flush()


# XXX: tmp.icc - pick proper one and ship it with the tool, or embed it
def write_pdfa(to_pdf):
    srgbxref = to_pdf._getNewXref()
    to_pdf.updateObject(srgbxref, """
<<
      /Alternate /DeviceRGB
      /N 3
>>
""")
    icc = pkg_resources.resource_string('internetarchivepdf', "data/tmp.icc")
    to_pdf.updateStream(srgbxref, icc, new=True)

    intentxref = to_pdf._getNewXref()
    to_pdf.updateObject(intentxref, """
<<
  /Type /OutputIntent
  /S /GTS_PDFA1
  /OutputConditionIdentifier (Custom)
  /Info (sRGB IEC61966-2.1)
  /DestOutputProfile %d 0 R
>>
""" % srgbxref)

    catalogxref = to_pdf.PDFCatalog()
    s = to_pdf.xrefObject(to_pdf.PDFCatalog())
    s = s[:-2]
    s += '  /OutputIntents [ %d 0 R ]' % intentxref
    s += '>>'
    to_pdf.updateObject(catalogxref, s)


def write_page_labels(to_pdf, scandata, errors=None):
    page_numbers = scandata_xml_get_page_numbers(scandata)
    res, all_ok = parse_series(page_numbers)

    # Add warning/error
    if errors is not None and not all_ok:
        errors.add(RECODE_RUNTIME_WARNING_INVALID_PAGE_NUMBERS)

    catalogxref = to_pdf.PDFCatalog()
    s = to_pdf.xrefObject(to_pdf.PDFCatalog())
    s = s[:-2]
    s += series_to_pdf(res)
    s += '>>'
    to_pdf.updateObject(catalogxref, s)



def write_basic_ua(to_pdf, language=None):
    # Create StructTreeRoot and descendants, allocate new xrefs as needed
    structtreeroot_xref = to_pdf._getNewXref()
    parenttree_xref = to_pdf._getNewXref()
    page_info_xrefs = []
    page_info_a_xrefs = []
    parenttree_kids_xrefs = []
    parenttree_kids_indirect_xrefs = []

    kids_cnt = ceil(to_pdf.pageCount / 32)
    for _ in range(kids_cnt):
        kid_xref = to_pdf._getNewXref()
        parenttree_kids_xrefs.append(kid_xref)

    # Parent tree contains a /Kids entry with a list of xrefs, that each contain
    # a list of xrefs (limited to 32 per), and each entry in that list of list
    # of xrefs contains a single reference that points to the page info xref.
    for idx, page in enumerate(to_pdf):
        page_info_xref = to_pdf._getNewXref()
        page_info_xrefs.append(page_info_xref)

        page_info_a_xref = to_pdf._getNewXref()
        page_info_a_xrefs.append(page_info_a_xref)

        parenttree_kids_indirect_xref = to_pdf._getNewXref()
        parenttree_kids_indirect_xrefs.append(parenttree_kids_indirect_xref)


    for idx in range(kids_cnt):
        start = idx*32
        stop = (idx+1)*31
        if stop > to_pdf.pageCount:
            stop = to_pdf.pageCount - 1

        s = """<<
  /Limits [ %d %d ]
""" % (start, stop - 1)
        s += '  /Nums [ '

        for pidx in range(start, stop):
            s += '%d %d 0 R ' % (pidx, parenttree_kids_indirect_xrefs[pidx])

            if idx % 7 == 0:
                s = s[:-1] + '\n' + '      '

        s += ']\n>>'

        to_pdf.updateObject(parenttree_kids_xrefs[idx], s)


    for idx, page in enumerate(to_pdf):
        intrect = tuple([int(x) for x in page.rect])

        s = """<<
  /BBox [ %d %d %d %d ]
  /InlineAlign /Center
  /O /Layout
  /Placement /Block
>>
""" % intrect
        to_pdf.updateObject(page_info_a_xrefs[idx], s)

        s = """ <<
  /A %d 0 R
  /K 0
  /P %d 0 R
  /Pg %d 0 R
  /S /Figure
>>""" % (page_info_a_xrefs[idx], structtreeroot_xref, page.xref)

        to_pdf.updateObject(page_info_xrefs[idx], s)


    for idx, page in enumerate(to_pdf):
        s = '[ %d 0 R ]' % page_info_a_xrefs[idx]
        to_pdf.updateObject(parenttree_kids_indirect_xrefs[idx], s)


    K = '  /Kids [ '
    for idx in range(kids_cnt):
        K += '%d 0 R ' % parenttree_kids_xrefs[idx]

        if idx % 7 == 0:
            K = K[:-1] + '\n' + '      '

    K += ']'
    s = """<<
%s
>>
""" % K

    to_pdf.updateObject(parenttree_xref, s)

    K = '  /K [ '
    for idx, xref in enumerate(page_info_xrefs):
        K += '%d 0 R ' % xref

        if idx % 7 == 0:
            K = K[:-1] + '\n' + '      '

    K += ']'

    to_pdf.updateObject(structtreeroot_xref, """
<<
""" + K + """
  /Type /StructTreeRoot
  /ParentTree %d 0 R
>>
""" % parenttree_xref)

    #  TODO? /ClassMap 1006 0 R
    #  TODO? /ParentTreeNextKey 198


    # Update pages, add back xrefs
    for idx, page in enumerate(to_pdf):
        page_data = to_pdf.xrefObject(page.xref)
        page_data = page_data[:-2]

        page_data += """
  /StructParents %d
""" % idx

        page_data += """
  /CropBox [ 0 0 %.1f %.1f ]
""" % (page.rect[2], page.rect[3])

        page_data += """
  /Rotate 0
"""
        page_data += """
  /Tabs /S
"""
        page_data += '>>'
        to_pdf.updateObject(page.xref, page_data)

    catalogxref = to_pdf.PDFCatalog()
    s = to_pdf.xrefObject(to_pdf.PDFCatalog())
    s = s[:-2]
    s += """
  /ViewerPreferences <<
    /FitWindow true
    /DisplayDocTitle true
  >>
"""
    if language:
        s += """
  /Lang (%s)
""" % language

    s += """
  /MarkInfo <<
    /Marked true
  >>
"""
    s += """
  /StructTreeRoot %d 0 R
""" % structtreeroot_xref

    s += '>>'
    to_pdf.updateObject(catalogxref, s)



def write_metadata(from_pdf, to_pdf, extra_metadata):
    doc_md = from_pdf.metadata if from_pdf is not None else {}

    doc_md['producer'] = PRODUCER

    if 'url' in extra_metadata:
        doc_md['keywords'] = extra_metadata['url']
    if 'title' in extra_metadata:
        doc_md['title'] = extra_metadata['title']
    if 'author' in extra_metadata:
        doc_md['author'] = extra_metadata['author']
    if 'creator' in extra_metadata:
        doc_md['creator'] = extra_metadata['creator']
    if 'subject' in extra_metadata:
        doc_md['subject'] = extra_metadata['subject']

    current_time = 'D:' + datetime.utcnow().strftime('%Y%m%d%H%M%SZ')
    if from_pdf is not None:
        doc_md['creationDate'] = from_pdf.metadata['creationDate']
    else:
        doc_md['creationDate'] = current_time
    doc_md['modDate'] = current_time

    # Set PDF basic metadata
    to_pdf.setMetadata(doc_md)

    have_xmlmeta = (from_pdf is not None) and (from_pdf._getXmlMetadataXref() > 0)
    if have_xmlmeta:
        xml_xref = from_pdf._getXmlMetadataXref()

        # Just copy the existing XML, perform no validity checks
        xml_bytes = from_pdf.xrefStream(xml_xref)
        to_pdf.setXmlMetadata(xml_bytes.decode('utf-8'))
    else:
        current_time = datetime.utcnow().isoformat(timespec='seconds') + 'Z'

        stream='''<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
        <x:xmpmeta xmlns:x="adobe:ns:meta/">
          <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
            <rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/">
              <xmp:CreateDate>{createdate}</xmp:CreateDate>
              <xmp:MetadataDate>{metadatadate}</xmp:MetadataDate>
              <xmp:ModifyDate>{modifydate}</xmp:ModifyDate>
              <xmp:CreatorTool>{creatortool}</xmp:CreatorTool>
            </rdf:Description>
            <rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">'''.format(creatortool=xmlescape(extra_metadata.get('creatortool', PRODUCER)),
           createdate=current_time, metadatadate=current_time,
           modifydate=current_time)

        if extra_metadata.get('title'):
            stream += '''
              <dc:title>
                <rdf:Alt>
                  <rdf:li xml:lang="x-default">{title}</rdf:li>
                </rdf:Alt>
              </dc:title>'''.format(title=xmlescape(extra_metadata.get('title')))

        # "An entity responsible for making the resource."
        # https://www.dublincore.org/specifications/dublin-core/dcmi-terms/#http://purl.org/dc/terms/creator
        # So should be author...
        if extra_metadata.get('author'):
            stream += '''
              <dc:creator>
                <rdf:Seq>
                  <rdf:li>{author}</rdf:li>
                </rdf:Seq>
              </dc:creator>'''.format(author=xmlescape(extra_metadata.get('author')))

        # TODO: Support multiple languages here?

        if extra_metadata.get('language'):
        # Empty language field means unknown language
            stream += '''
              <dc:language>
                <rdf:Bag>'''

            for language in extra_metadata.get('language', []):
                stream += '''
                  <rdf:li>{language}</rdf:li>'''.format(language=xmlescape(language))

            stream += '''
                </rdf:Bag>
              </dc:language>'''

        stream += '''
            </rdf:Description>
            <rdf:Description rdf:about="" xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/">
              <pdfaid:part>3</pdfaid:part>
              <pdfaid:conformance>B</pdfaid:conformance>
            </rdf:Description>
          </rdf:RDF>
        </x:xmpmeta>
        <?xpacket end="r"?>'''

        to_pdf.setXmlMetadata(stream)


#  pymupdf inserts stuff like '/Author (none)' when the author is not provided.
#  This is wrong. We'll file a bug, but let's first fix it here.
def fixup_pymupdf_metadata(doc):
    # Access to the Info xref is not in the API, so let's dig for it.
    trailer_lines = doc.PDFTrailer().split('\n')
    for line in trailer_lines:
        if '  /Info ' in line:
            s = line.replace('  /Info ', '')
            info_xref = s[:s.find(' ')]
            info_xref = int(info_xref)

            s = doc.xrefObject(info_xref)

            new_s = ''

            for infoline in s.split('\n'):
                if re.match('^.*\/[A-Za-z]+ \(none\)$', infoline):
                    continue

                new_s += infoline + '\n'

            doc.updateObject(info_xref, new_s)

            break


# TODO: Document these options (like in bin/recode_pdf)
def recode(from_pdf=None, from_imagestack=None, dpi=None, hocr_file=None,
        scandata_file=None, out_pdf=None, out_dir=None,
        reporter=None,
        grayscale_pdf=False,
        image_mode=IMAGE_MODE_MRC, jbig2=False, verbose=False, tmp_dir=None,
        report_every=None, stop_after=None,
        bg_slope=47000, fg_slope=49000,
        bg_downsample=None,
        denoise_mask=None,
        hq_pages=None,
        hq_bg_slope=47000, hq_fg_slope=47000,
        metadata_url=None, metadata_title=None, metadata_author=None,
        metadata_creator=None, metadata_language=None,
        metadata_subject=None, metadata_creatortool=None):
    # TODO: document that the scandata document dpi will override the dpi arg
    # TODO: Take hq-pages and reporter arg and change format (as lib call we
    # don't want to pass that as one string, I guess?)

    errors = set()

    in_pdf = None
    if from_pdf:
        in_pdf = fitz.open(from_pdf)

    image_files = None
    if from_imagestack:
        image_files = sorted(glob(from_imagestack))

    hocr_file = hocr_file
    outfile = out_pdf

    stop = stop_after
    if stop is not None:
        stop -= 1

    if verbose:
        from numpy.core._multiarray_umath import __cpu_features__ as cpu_have
        cpu = cpu_have
        for k, v in cpu.items():
            if v:
                print('\t', k)


    reporter = reporter.split(' ') if reporter else None # TODO: overriding

    start_time = time()

    scandata_doc_dpi = None

    # Figure out if we have scandata, and figure out if we want to skip pages
    # based on scandata.
    skip_pages = []
    if scandata_file is not None:
        skip_pages = scandata_xml_get_skip_pages(scandata_file)
        dpi_pages = scandata_xml_get_dpi_per_page(scandata_file)
        scandata_doc_dpi = scandata_xml_get_document_dpi(scandata_file)

        if scandata_doc_dpi is not None:
            # Let's prefer the DPI in the scandata file over the provided DPI
            dpi = scandata_doc_dpi

    # XXX: Maybe use a buffer, since the file is typically quite small
    fd, tess_tmp_path = mkstemp(prefix='pdfrenderer', suffix='.pdf', dir=tmp_dir)
    os.close(fd)

    if verbose:
        print('Creating text only PDF')

    # 1. Create text-only PDF from hOCR first, but honour page sizes of in_pdf
    create_tess_textonly_pdf(hocr_file, tess_tmp_path, in_pdf=in_pdf,
            image_files=image_files, dpi=dpi,
            skip_pages=skip_pages, dpi_pages=dpi_pages,
            reporter=reporter,
            verbose=verbose, stop_after=stop,
            errors=errors)

    if verbose:
        print('Inserting (and compressing) images')
    # 2. Load tesseract PDF and stick images in the PDF
    # We open the generated file but do not modify it in place
    outdoc = fitz.open(tess_tmp_path)

    HQ_PAGES = [False for x in range(outdoc.pageCount)]
    if hq_pages is not None:
        index_range = map(int, hq_pages.split(','))
        for i in index_range:
            # We want 0-indexed, not 1-indexed, but not negative numbers we want
            # to remain 1-indexed.
            if i > 0:
                i = i - 1

            if abs(i) >= len(HQ_PAGES):
                # Page out of range, silently ignore for automation purposes.
                # We don't want scripts that call out tool to worry about how
                # many a PDF has exactly. E.g. if 1,2,3,4,-4,-3,-2,-1 is passed,
                # and a PDF has only three pages, let's just set them all to HQ
                # and not complain about 4 and -4 being out of range.
                continue

            # Mark page as HQ
            HQ_PAGES[i] = True


    if verbose:
        print('Converting with image mode:', image_mode)
    if image_mode == 2:
        insert_images_mrc(outdoc, hocr_file,
                          from_pdf=in_pdf,
                          image_files=image_files,
                          bg_slope=bg_slope,
                          fg_slope=fg_slope,
                          skip_pages=skip_pages,
                          img_dir=out_dir,
                          jbig2=jbig2,
                          bg_downsample=bg_downsample,
                          denoise_mask=denoise_mask,
                          reporter=reporter,
                          hq_pages=HQ_PAGES,
                          hq_bg_slope=hq_bg_slope,
                          hq_fg_slope=hq_fg_slope,
                          verbose=verbose,
                          tmp_dir=tmp_dir,
                          report_every=report_every,
                          stop_after=stop,
                          grayscale_pdf=grayscale_pdf)
    elif image_mode in (0, 1):
        # TODO: Update this codepath
        insert_images(in_pdf, outdoc, mode=image_mode,
                report_every=report_every, stop_after=stop)
    elif image_mode == 3:
        # 3 = skip
        pass

    # 3. Add PDF/A compliant data
    write_pdfa(outdoc)

    if scandata_file is not None:
        # XXX: we parse scandata twice now, let's not do that
        # 3b. Write page labels from scandata file, if present
        write_page_labels(outdoc, scandata_file, errors=errors)


    lang_if_any = metadata_language[0] if metadata_language else None
    write_basic_ua(outdoc, language=lang_if_any)

    # 4. Write metadata
    extra_metadata = {}
    if metadata_url:
        extra_metadata['url'] = metadata_url
    if metadata_title:
        extra_metadata['title'] = metadata_title
    if metadata_creator:
        extra_metadata['creator'] = metadata_creator
    if metadata_author:
        extra_metadata['author'] = metadata_author
    if metadata_language:
        extra_metadata['language'] = metadata_language
    if metadata_subject:
        extra_metadata['subject'] = metadata_subject
    if metadata_creatortool:
        extra_metadata['creatortool'] = metadata_creatortool
    write_metadata(in_pdf, outdoc, extra_metadata=extra_metadata)

    print('Fixing up pymupdf metadata')
    fixup_pymupdf_metadata(outdoc)

    # 5. Save
    if verbose:
        print('mupdf warnings, if any:', repr(fitz.TOOLS.mupdf_warnings()))
    if verbose:
        print('Saving PDF now')

    t = time()
    outdoc.save(outfile, deflate=True, pretty=True)
    save_time_ms = int((time() - t)*1000)
    if reporter:
        data = json.dumps({'time_to_save': {'time': save_time_ms}})
        subprocess.check_output(reporter, input=data.encode('utf-8'))

    end_time = time()
    print('Processed %d pages at %.2f seconds/page' % (len(outdoc),
        (end_time - start_time) / len(outdoc)))

    if from_pdf is not None:
        oldsize = os.path.getsize(from_pdf)
    else:
        bytesum = 0
        skipped_pages = 0
        for idx, fname in enumerate(image_files):
            if skip_pages is not None and idx in skip_pages:
                skipped_pages += 1
                continue

            if stop_after is not None and (idx - skipped_pages) > stop_after:
                break

            bytesum += os.path.getsize(fname)

        oldsize = bytesum

    newsize = os.path.getsize(out_pdf)
    compression_ratio  = oldsize / newsize
    if verbose:
        print('Compression ratio: %f' % (compression_ratio))

    # 5. Remove leftover files
    remove(tess_tmp_path)

    return {'errors': errors,
            'compression_ratio': compression_ratio}