"""Web server that has endpoints to write aligned data to cloud storage.
"""

import os

from flask import Flask, Response, request, make_response, abort
from flask_cors import CORS
import json
import logging
import pwd
from PIL import Image
from google.cloud import storage
import numpy as np
import tensorstore as ts
from math import ceil
from scipy import ndimage
import io
import traceback
import threading
from skimage import exposure

# allow very large images to be read (up to 1 gigavoxel)
Image.MAX_IMAGE_PIXELS = 1000000000

app = Flask(__name__)

# TODO: Limit origin list here: CORS(app, origins=[...])
CORS(app)
logger = logging.getLogger(__name__)

MAX_IMAGE_SIZE = 4096

@app.route('/alignedslice', methods=["POST"])
def alignedslice():
    """Read images storeed in bucket/raw/image, apply the affine transformation
    and write result to bucket/align/image and bucket_temp/slice.
    """
    try:
        config_file  = request.get_json()
        
        name = config_file["img"] 
        bucket_name = config_file["dest"] # contains source and destination
        bucket_name_temp = config_file["dest-tmp"] # destination for tiles
        affine_trans = json.loads(config_file["transform"])
        [width, height]  = json.loads(config_file["bbox"])
        slicenum  = config_file["slice"]
        shard_size  = config_file["shard-size"]

        # read file
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob("raw/" + name)
        pre_image_bin = blob.download_as_string()
        curr_im = Image.open(io.BytesIO(pre_image_bin))
        del pre_image_bin

        # modify affine to satisfy the pil transform interface
        # (origin should be center -- not the case actually, row1 then row2, and use inverse affine
        # since transform implements a pull transform and not a push transform).
        # create affine matrix and invert
        affine_mat = np.array([[affine_trans[0], affine_trans[2], affine_trans[4]],
                [affine_trans[1], affine_trans[3], affine_trans[5]],
                [0, 0, 1]])
        mat_inv = np.linalg.inv(affine_mat)
        curr_im = curr_im.transform((width, height), Image.AFFINE, data=mat_inv.flatten()[:6], resample=Image.BICUBIC)
         
        # write aligned png as a much smaller thumbnail
        # (mostly for debugging or quick viewing in something like fiji)
        blob = bucket.blob("align/" + name)
        TARGET_SIZE = 4096
        with io.BytesIO() as output:
            max_dim = max(width, height)
            factor = 1
            while max_dim > TARGET_SIZE:
                max_dim = max_dim // 2
                factor *= 2
            im_small = curr_im
            if factor > 1:
                im_small = curr_im.resize((width//factor, height//factor), resample=Image.BICUBIC)
            
            # normalize image (even though potentially downsampled heavily)
            im_small = Image.fromarray((exposure.equalize_adapthist(np.array(im_small), kernel_size=1024)*255).astype(np.uint8))
        
            # write output to bucket
            im_small.save(output, format="PNG")
            blob.upload_from_string(output.getvalue(), content_type="image/png")

        NUM_THREADS = 4
        # write sub-image into tile chunks (group together to reduce IO)
        # TODO: add overlap betwen tiles for CLAHE calculation
        failure = None
        def write_sub_image_tiles(thread_id):
            nonlocal failure
            try:
                storage_client = storage.Client()
                bucket = storage_client.bucket(bucket_name)
                # write temp png tiles
                job_id = -1
                
                for starty in range(0, height, MAX_IMAGE_SIZE):
                    for startx in range(0, width, MAX_IMAGE_SIZE):
                        # determine which thread gets the job
                        job_id += 1
                        if (job_id % NUM_THREADS) != thread_id:
                            continue

                        binary_volume = "".encode()
                        sizes = []
                        for chunky in range(starty, min(starty+MAX_IMAGE_SIZE, height), shard_size):
                            for chunkx in range(startx, min(startx+MAX_IMAGE_SIZE, width), shard_size):
                                tile = np.array(curr_im.crop((chunkx, chunky, chunkx+shard_size, chunky+shard_size)))
                                tile = (exposure.equalize_adapthist(tile, kernel_size=1024)*255).astype(np.uint8)
                                tile_bytes_io = io.BytesIO()
                                # save as png
                                tile_im = Image.fromarray(tile)
                                tile_im.save(tile_bytes_io, format="PNG")
                                tile_bytes = tile_bytes_io.getvalue()

                                sizes.append(len(tile_bytes))
                                binary_volume += tile_bytes

                        # pack binary
                        final_binary = width.to_bytes(8, byteorder="little")
                        final_binary += height.to_bytes(8, byteorder="little")
                        final_binary += shard_size.to_bytes(8, byteorder="little")

                        start_pos = 24 + (len(sizes)+1)*8
                        final_binary += start_pos.to_bytes(8, byteorder="little")
                        for val in sizes:
                            start_pos += val
                            final_binary += start_pos.to_bytes(8, byteorder="little")
                        final_binary += binary_volume

                        # write to cloud
                        bucket_temp = storage_client.bucket(bucket_name_temp)
                        blob = bucket_temp.blob(f"{slicenum}_{startx//MAX_IMAGE_SIZE}_{starty//MAX_IMAGE_SIZE}")
                        blob.upload_from_string(final_binary, content_type="application/octet-stream")
            except Exception as e:
                failure = e
        # write superblocks to disk in chunks of MAX_IMAGE_SIZE
        threads = [threading.Thread(target=write_sub_image_tiles, args=(thread_id,)) for thread_id in range(NUM_THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        #write_sub_image_tiles(0)
        if failure is not None:
            raise failure

        r = make_response("success".encode())
        r.headers.set('Content-Type', 'text/html')
        return r
    except Exception as e:
        return Response(str(e), 400)

@app.route('/ngmeta', methods=["POST"])
def ngmeta():
    """Write metadata for ng volumes in bucket/neuroglancer/raw and bucket/neuroglancer/jpeg.
    """
    try:
        config_file  = request.get_json()
        bucket_name = config_file["dest"] # contains source and destination
        minz  = int(config_file["minz"])
        maxz  = int(config_file["maxz"])
        res = int(config_file["resolution"])
        [width, height]  = json.loads(config_file["bbox"])
        shard_size  = config_file["shard-size"] 
        if shard_size != 1024:
            raise RuntimeError("shard size must be 1024x1024x1024")
        write_raw  = json.loads(config_file["writeRaw"].lower())

        # write jpeg config to bucket/neuroglancer/raw/info
        storage_client = storage.Client()
        config = create_meta(width, height, minz, maxz, shard_size, False, res)
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob("neuroglancer/jpeg/info")
        blob.upload_from_string(json.dumps(config))
       
        # write raw config to bucket/neuroglancer/raw/info
        if write_raw:
            config = create_meta(width, height, minz, maxz, shard_size, True, res)
            blob = bucket.blob("neuroglancer/raw/info")
            blob.upload_from_string(json.dumps(config))

        r = make_response("success".encode())
        r.headers.set('Content-Type', 'text/html')
        return r
    except Exception as e:
        return Response(str(e), 400)

@app.route('/ngshard', methods=["POST"])
def ngshard():
    """Write ng pyramid to bucket/neuroglancer/raw and bucket/neuroglancer/jpeg.
    """
    try:
        config_file  = request.get_json()
        
        bucket_name = config_file["dest"] # contains source and destination
        bucket_tiled_name = config_file["source"] # contains image tiles
        tile_chunk = config_file["start"]
        minz  = config_file["minz"]
        maxz  = config_file["maxz"]
        [width, height]  = json.loads(config_file["bbox"])
        shard_size  = config_file["shard-size"] 
        if shard_size != 1024:
            raise RuntimeError("shard size must be 1024x1024x1024")
        write_raw  = json.loads(config_file["writeRaw"].lower())

        # extract 1024x1024x1024 cube based on tile chunk
        zstart = max(shard_size*tile_chunk[2], minz)
        zfinish = min(maxz, zstart+shard_size-1)
    
        storage_client = storage.Client()
        bucket_temp = storage_client.bucket(bucket_tiled_name)
        
        vol3d = None

        assert((MAX_IMAGE_SIZE % shard_size) == 0)
        def set_image(slice):
            nonlocal vol3d
            
            # x and y block location
            x_block = (tile_chunk[0]*shard_size) // MAX_IMAGE_SIZE
            y_block = (tile_chunk[1]*shard_size) // MAX_IMAGE_SIZE

            # setup offsets for finding shards
            chunk_tile_chunk_0 = ((tile_chunk[0]*shard_size) %  MAX_IMAGE_SIZE) // shard_size
            chunk_tile_chunk_1 = ((tile_chunk[1]*shard_size) %  MAX_IMAGE_SIZE) // shard_size
            chunk_width = ceil(min(MAX_IMAGE_SIZE, (width-(tile_chunk[0]*shard_size) ) ) / shard_size)

            # get image block
            blob = bucket_temp.blob(str(slice))
            blob = bucket_temp.blob(f"{slice}_{x_block}_{y_block}")
            
            # read offset binary
            pre = 24 # start of index
        
            spot = chunk_tile_chunk_1*chunk_width + chunk_tile_chunk_0
            start_index = pre + spot * 8
            end_index = start_index + 16 - 1

            im_range = blob.download_as_string(start=start_index, end=end_index)
            start = int.from_bytes(im_range[0:8], byteorder="little")
            end = int.from_bytes(im_range[8:16], byteorder="little", signed=False) - 1
            
            # png blob
            im_data = blob.download_as_string(start=start, end=end)
            im = Image.open(io.BytesIO(im_data))
            img_array = np.array(im)
            height2, width2 = im.height, im.width
           
            #with io.BytesIO() as output:
            #    blob = bucket_temp.blob(str(slice)+".png")
            #    im.save(output, format="PNG")
            #    blob.upload_from_string(output.getvalue(), content_type="image/png")

            if slice == zstart:
                vol3d = np.zeros((zfinish-zstart+1, height2, width2), dtype=np.uint8)
            vol3d[(slice-zstart), :, :] = img_array
        
        # sest first image
        set_image(zstart)

        # fetch 1024x1024 tile from each imagee
        def set_images(start, finish, thread_id, num_threads):
            for slice in range(start, finish+1):
                if (slice % num_threads) == thread_id:
                    set_image(slice)

        # use 20 threads in parallel to fetch
        num_threads = 20
        threads = [threading.Thread(target=set_images, args=(zstart+1, zfinish, thread_id, num_threads)) for thread_id in range(num_threads)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    
        # write grayscale for each level
        num_levels = 6
        start = (tile_chunk[0]*shard_size, tile_chunk[1]*shard_size, zstart)

        # put in fortran order
        vol3d = vol3d.transpose((2,1,0))

        def _write_shard(level, start, vol3d, format, dataset=None):
            """Method to write shard through tensorstore.
            """

            if dataset is None:
                # get spec for jpeg and post
                dataset = ts.open({
                    'driver': 'neuroglancer_precomputed',
                    'kvstore': {
                        'driver': 'gcs',
                        'bucket': bucket_name,
                        },
                    'path': f"neuroglancer/{format}",
                    'recheck_cached_data': 'open',
                    'scale_index': level
                }).result()
                dataset = dataset[ts.d['channel'][0]]

            size = vol3d.shape
            dataset[ start[0]:(start[0]+size[0]), start[1]:(start[1]+size[1]), start[2]:(start[2]+size[2]) ] = vol3d 
            return dataset 

        def _downsample(vol):
            """Downsample piecewise.
            """
            x,y,z = vol.shape

            # just call interpolate over whole volume if large enough
            if x <= 256 and y <= 256 and z <= 256:
                return ndimage.interpolation.zoom(vol, 0.5, order=1)
            
            target = np.zeros((round(x/2),round(y/2),round(z/2)), dtype=np.uint8)
            for xiter in range(0, x, 256):
                for yiter in range(0, y, 256):
                    for ziter in range(0, z, 256):
                        target[(xiter//2):((xiter+256)//2), (yiter//2):((yiter+256)//2), (ziter//2):((ziter+256)//2)] = ndimage.interpolation.zoom(vol[xiter:(xiter+256),yiter:(yiter+256),ziter:(ziter+256)], 0.5, order=1)
            return target 
        
        #storage_client2 = storage.Client()
        #bucket = storage_client2.bucket(bucket_name)

        for level in range(num_levels):
            if level == 0:
                # iterate through different 512 cubes since 1024 will not fit in memory
                dataset_jpeg = None
                dataset_raw = None
                for iterz in range(0, 1024, 512):
                    for itery in range(0, 1024, 512):
                        for iterx in range(0, 1024, 512):
                            vol3d_temp = vol3d[iterx:(iterx+512), itery:(itery+512), iterz:(iterz+512)]
                            currsize = vol3d_temp.shape
                            if currsize[0] == 0 or currsize[1] == 0 or currsize[2] == 0:
                                continue
                            start_temp = (start[0]+iterx, start[1]+itery, start[2]+iterz) 
                            
                            dataset_jpeg = _write_shard(level, start_temp, vol3d_temp, "jpeg", dataset_jpeg)
                            if write_raw:
                                # zoffset is not correctly set !!
                                #blob = bucket.blob(f"chunks/{start[0]}-{start[0]+512}_{start[1]}-{start[1]+512}_{start[2]}-{start[2]+512}")
                                #tarr = np.zeros((512, 512, 512), dtype=np.uint8)
                                #tarr[0:vol3d_temp.shape[0], 0:vol3d_temp.shape[1], 0:vol3d_temp.shape[2]] = vol3d_temp

                                #blob.upload_from_string(tarr.tostring(), content_type="application/octet-stream")
                                 

                                dataset_raw = _write_shard(level, start_temp, vol3d_temp, "raw", dataset_raw)
            else:
                _write_shard(level, start, vol3d, "jpeg")

            # downsample
            #vol3d = ndimage.interpolation.zoom(vol3d, 0.5)
            vol3d = _downsample(vol3d)
            start = (start[0]//2, start[1]//2, start[2]//2)
            currsize = vol3d.shape
            if currsize[0] == 0 or currsize[1] == 0 or currsize[2] == 0:
                break

        r = make_response("success".encode())
        r.headers.set('Content-Type', 'text/html')
        return r
    except Exception as e:
        return Response(traceback.format_exc(), 400)

def create_meta(width, height, minz, maxz, shard_size, isRaw, res):
    if (width % shard_size) > 0: 
        width += ( 1024 - (width % shard_size))
    if (height % shard_size) > 0: 
        height += ( 1024 - (height % shard_size))
    if ((maxz + 1)  % shard_size) > 0: 
        maxz += ( 1024 - ((maxz+1) % shard_size))

    # !! makes offset 0 since there appears to be a bug in the
    # tensortore driver.

    # !! make jpeg chunks 256 cubes (return to 512, maybe,
    # when tensorstore issues are addressed)

    # !! refactor to use unsharded format for raw (just save
    # 256 chunks) and for 64 and 128 cubes for jpeg (currently
    # a bug with unsharded pieces in ng)

    if isRaw:
        return {
                "@type" : "neuroglancer_multiscale_volume",
                "data_type" : "uint8",
                "num_channels" : 1,
                "scales" : [
                    {
                        "chunk_sizes" : [
                            [ 128, 128, 128 ]
                            ],
                        "encoding" : "raw",
                        "key" : f"{res}.0x{res}.0x{res}.0",
                        "resolution" : [ res, res, res ],
                        "size" : [ width, height, (maxz+1) ],
                        "realsize" : [ width, height, (maxz-minz+1) ],
                        "offset" : [0, 0, 0],
                        "realoffset" : [0, 0, minz]
                    }
                ],
                "type" : "image"
            }

    # load json (don't need tensorflow)
    return {
       "@type" : "neuroglancer_multiscale_volume",
       "data_type" : "uint8",
       "num_channels" : 1,
       "scales" : [
          {
             "chunk_sizes" : [
                [ 64, 64, 64 ]
             ],
             "encoding" : "raw" if isRaw else "jpeg",
             "key" : f"{res}.0x{res}.0x{res}.0",
             "resolution" : [ res, res, res ],
             "sharding" : {
                "@type" : "neuroglancer_uint64_sharded_v1",
                "hash" : "identity",
                "minishard_bits" : 0,
                "minishard_index_encoding" : "gzip",
                "preshift_bits" : 6,
                "shard_bits" : 27
             },
             "size" : [ width, height, (maxz+1) ],
             "realsize" : [ width, height, (maxz-minz+1) ],
             "offset" : [0, 0, 0],
             "realoffset" : [0, 0, minz]
          },
          {
             "chunk_sizes" : [
                [ 64, 64, 64 ]
             ],
             "encoding" : "raw" if isRaw else "jpeg",
             "key" : f"{res*2}.0x{res*2}.0x{res*2}.0",
             "resolution" : [ res*2, res*2, res*2 ],
             "sharding" : {
                "@type" : "neuroglancer_uint64_sharded_v1",
                "hash" : "identity",
                "minishard_bits" : 0,
                "minishard_index_encoding" : "gzip",
                "preshift_bits" : 6,
                "shard_bits" : 24
             },
             "size" : [ width//2+1, height//2+1, (maxz+1)//2+1 ],
             "realsize" : [ width//2, height//2, (maxz-minz+1)//2 ],
             "offset" : [0, 0, 0],
             "realoffset" : [0, 0, minz//2]
          },
          {
             "chunk_sizes" : [
                [ 64, 64, 64 ]
             ],
             "encoding" : "raw" if isRaw else "jpeg",
             "key" : f"{res*4}.0x{res*4}.0x{res*4}.0",
             "resolution" : [ res*4, res*4, res*4 ],
             "sharding" : {
                "@type" : "neuroglancer_uint64_sharded_v1",
                "hash" : "identity",
                "minishard_bits" : 0,
                "minishard_index_encoding" : "gzip",
                "preshift_bits" : 6,
                "shard_bits" : 21
             },
             "size" : [ width//4+2, height//4+2, (maxz+1)//4+2 ],
             "realsize" : [ width//4, height//4, (maxz-minz+1)//4 ],
             "offset" : [0, 0, 0],
             "realoffset" : [0, 0, minz//4]
          },
          {
             "chunk_sizes" : [
                [ 64, 64, 64 ]
             ],
             "encoding" : "raw" if isRaw else "jpeg",
             "key" : f"{res*8}.0x{res*8}.0x{res*8}.0",
             "resolution" : [ res*8, res*8, res*8 ],
             "size" : [ width//8+4, height//8+4, (maxz+1)//8+4 ],
             "realsize" : [ width//8, height//8, (maxz-minz+1)//8 ],
             "offset" : [0, 0, 0],
             "realoffset" : [0, 0, minz//8]
          },
          {
             "chunk_sizes" : [
                [ 64, 64, 64 ]
             ],
             "encoding" : "raw" if isRaw else "jpeg",
             "key" : f"{res*16}.0x{res*16}.0x{res*16}.0",
             "resolution" : [ res*16, res*16, res*16 ],
             "size" : [ width//16+8, height//16+8, (maxz+1)//16+8 ],
             "realsize" : [ width//16, height//16, (maxz-minz+1)//16 ],
             "offset" : [0, 0, 0],
             "realoffset" : [0, 0, minz//16]
          },
          {
             "chunk_sizes" : [
                [ 64, 64, 64 ]
             ],
             "encoding" : "raw" if isRaw else "jpeg",
             "key" : f"{res*32}.0x{res*32}.0x{res*32}.0",
             "resolution" : [ res*32, res*32, res*32 ],
             "size" : [ width//32+16, height//32+16, (maxz+1)//32+16 ],
             "realsize" : [ width//32, height//32, (maxz-minz+1)//32 ],
             "offset" : [0, 0, 0],
             "realoffset" : [0, 0, minz//16]
          }
       ],
       "type" : "image"
    }



if __name__ == "__main__":
    app.run(debug=True,host='0.0.0.0',port=int(os.environ.get('PORT', 8080)))
